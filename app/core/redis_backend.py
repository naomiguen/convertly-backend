"""Penyimpan bersama berbasis Redis untuk rate limit dan batas pekerjaan serentak.

Dengan beberapa worker uvicorn / beberapa container, penghitung di memori proses tidak saling tahu,
sehingga batas efektifnya dikali jumlah worker. Redis membuat semua worker memakai satu hitungan.

Semua operasi atomik lewat skrip Lua (hanya perintah dasar: TIME, ZADD, ZREMRANGEBYSCORE, ZRANGE, ZCARD,
PEXPIRE). Waktu diambil dari `TIME` milik Redis, jadi jam antar worker tidak perlu sinkron.

Jika Redis tidak terjangkau, `manager.get()` mengembalikan None dan pemanggil memakai penghitung memori;
koneksi dicoba lagi tiap REDIS_RETRY_SECONDS. API tidak pernah mati hanya karena Redis mati.
"""
import logging
import threading
import time
import uuid

import redis

from app.core.config import settings

logger = logging.getLogger("convertly")


class RedisUnavailable(Exception):
    """Redis tidak bisa dipakai saat ini; pemanggil harus fallback ke memori."""


# Sliding window berbiaya. KEYS[1]=sorted set; ARGV: biaya, batas, jendela (ms), token unik.
# Return -1 jika diizinkan (dan dicatat), selain itu detik yang harus ditunggu.
# Aturan yang sama dengan versi memori: request pertama pada jendela kosong selalu boleh.
_HIT_LUA = """
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
local cost = tonumber(ARGV[1])
local limit = tonumber(ARGV[2])
local window = tonumber(ARGV[3])
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now - window)
local entries = redis.call('ZRANGE', KEYS[1], 0, -1, 'WITHSCORES')
local used = 0
local costs = {}
for i = 1, #entries, 2 do
  local c = tonumber(string.match(entries[i], '|([^|]+)$')) or 0
  costs[#costs + 1] = c
  used = used + c
end
if #entries > 0 and used + cost > limit then
  local need = used + cost - limit
  local freed = 0
  for i = 1, #costs do
    freed = freed + costs[i]
    if freed >= need then
      local oldest = tonumber(entries[2 * i])
      return math.ceil((oldest + window - now) / 1000)
    end
  end
  return math.ceil(window / 1000)
end
redis.call('ZADD', KEYS[1], now, ARGV[4] .. '|' .. ARGV[1])
redis.call('PEXPIRE', KEYS[1], window)
return -1
"""

# Ambil satu slot pekerjaan bila masih ada tempat. KEYS[1]=sorted set (skor = waktu kedaluwarsa sewa).
# ARGV: batas maksimum, umur sewa (detik), token. Return 1 jika dapat slot, 0 jika penuh.
_ENTER_LUA = """
local now = tonumber(redis.call('TIME')[1])
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now)
if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[1]) then
  return 0
end
redis.call('ZADD', KEYS[1], now + tonumber(ARGV[2]), ARGV[3])
redis.call('EXPIRE', KEYS[1], tonumber(ARGV[2]) + 60)
return 1
"""


class RedisBackend:
    def __init__(self, client) -> None:
        self.client = client
        self._hit = client.register_script(_HIT_LUA)
        self._enter = client.register_script(_ENTER_LUA)
        # Muat skrip sekarang (dipanggil dari manager.get() yang menangani error koneksi), supaya request
        # pertama tidak perlu bolak-balik NOSCRIPT -> SCRIPT LOAD -> EVALSHA. Jika cache skrip Redis
        # terhapus (mis. Redis restart), redis-py tetap otomatis memuat ulang.
        client.script_load(_HIT_LUA)
        client.script_load(_ENTER_LUA)

    @staticmethod
    def _key(*parts: str) -> str:
        return ":".join((settings.REDIS_KEY_PREFIX, *parts))

    def _run(self, call):
        try:
            return call()
        except (redis.RedisError, OSError) as exc:
            manager.mark_down(exc)
            raise RedisUnavailable from exc

    # ---- rate limit
    def hit(self, bucket: str, ip: str, cost: float, limit: float, window: float) -> int | None:
        result = self._run(lambda: self._hit(
            keys=[self._key("rl", bucket, ip)],
            args=[repr(float(cost)), repr(float(limit)), int(window * 1000), uuid.uuid4().hex],
        ))
        wait = int(result)
        return None if wait < 0 else max(1, wait)

    # ---- slot pekerjaan
    def enter(self, scope: str, maximum: int, token: str) -> bool:
        return bool(self._run(lambda: self._enter(
            keys=[self._key("jobs", scope)], args=[maximum, settings.JOB_LEASE_SECONDS, token],
        )))

    def leave(self, scope: str, token: str) -> None:
        try:
            self.client.zrem(self._key("jobs", scope), token)
        except (redis.RedisError, OSError) as exc:
            # Gagal melepas bukan masalah besar: sewa kedaluwarsa sendiri. Cukup catat.
            logger.warning("Gagal melepas slot Redis (%s); akan kedaluwarsa sendiri", exc)

    def flush(self) -> None:
        """Hapus semua kunci milik aplikasi ini (dipakai tes / perawatan)."""
        keys = list(self.client.scan_iter(match=self._key("*")))
        if keys:
            self.client.delete(*keys)


class RedisManager:
    """Memegang koneksi, dan menandai Redis 'mati' sementara agar request tidak menunggu timeout terus-menerus."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._backend: RedisBackend | None = None
        self._backend_url = ""
        self._down_until = 0.0
        self._factory = None   # hanya untuk tes: fungsi pembuat client palsu

    def set_client_factory(self, factory) -> None:
        with self._lock:
            self._factory = factory
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self._backend = None
            self._backend_url = ""
            self._down_until = 0.0

    def _make_client(self, url: str):
        if self._factory is not None:
            return self._factory(url)
        return redis.Redis.from_url(
            url, decode_responses=True,
            socket_timeout=settings.REDIS_TIMEOUT_SECONDS,
            socket_connect_timeout=settings.REDIS_TIMEOUT_SECONDS,
        )

    def get(self) -> RedisBackend | None:
        """Backend Redis yang siap dipakai, atau None (tidak dikonfigurasi / sedang mati)."""
        url = settings.REDIS_URL
        if not url:
            return None
        with self._lock:
            if time.monotonic() < self._down_until:
                return None
            if self._backend is not None and self._backend_url == url:
                return self._backend
        try:
            client = self._make_client(url)
            client.ping()
            backend = RedisBackend(client)
        except (redis.RedisError, OSError) as exc:
            self.mark_down(exc)
            return None
        with self._lock:
            self._backend, self._backend_url = backend, url
        logger.info("Redis tersambung: rate limit dan batas pekerjaan dibagi antar worker")
        return backend

    def mark_down(self, exc: Exception) -> None:
        with self._lock:
            self._backend = None
            self._down_until = time.monotonic() + settings.REDIS_RETRY_SECONDS
        logger.warning(
            "Redis tidak tersedia (%s: %s). Memakai penghitung memori; dicoba lagi dalam %d detik.",
            type(exc).__name__, exc, settings.REDIS_RETRY_SECONDS,
        )

    def backend_name(self) -> str:
        return "redis" if self.get() is not None else "memory"


manager = RedisManager()
