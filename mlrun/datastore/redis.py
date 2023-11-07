# Copyright 2023 Iguazio
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from urllib.parse import urlparse

import redis
import redis.cluster

import mlrun

from .base import DataStore


class RedisStore(DataStore):
    """
    Partial implementation of DataStore over the Redis KV store

    - no support for filesystem
    - key and value sizes are limited to 512MB
    """

    def __init__(self, parent, schema, name, endpoint="", secrets: dict = None):
        REDIS_DEFAULT_PORT = "6379"
        super().__init__(parent, name, schema, endpoint, secrets=secrets)
        self.headers = None

        self.endpoint = self.endpoint or mlrun.mlconf.redis.url
        parsed_endpoint = urlparse(self.endpoint)
        if parsed_endpoint.scheme != "redis" and parsed_endpoint.scheme != "rediss":
            parsed_endpoint = urlparse(f"{schema}://{self.endpoint}")
        self.secure = parsed_endpoint.scheme == "rediss"

        if parsed_endpoint.username or parsed_endpoint.password:
            raise mlrun.errors.MLRunInvalidArgumentError(
                "Provide Redis username and password only via secrets"
            )
        user = self._get_secret_or_env("REDIS_USER", "")
        password = self._get_secret_or_env("REDIS_PASSWORD", "")
        host = parsed_endpoint.hostname
        port = parsed_endpoint.port if parsed_endpoint.port else REDIS_DEFAULT_PORT
        schema = parsed_endpoint.scheme
        if user or password:
            endpoint = f"{schema}://{user}:{password}@{host}:{port}"
        else:
            endpoint = f"{schema}://{host}:{port}"
        self._redis_url = endpoint

        self._redis = None

    @property
    def redis(self):
        if self._redis is None:
            try:
                self._redis = redis.cluster.RedisCluster.from_url(
                    self._redis_url, decode_responses=True
                )
            except redis.cluster.RedisClusterException:
                self._redis = redis.Redis.from_url(
                    self._redis_url, decode_responses=True
                )

        return self._redis

    def get_filesystem(self, silent):
        return None  # no support for fsspec

    def supports_isdir(self):
        return False

    @classmethod
    def build_redis_key(cls, key, prefix_only=False):
        prefixes = ["redis://", "rediss://", "ds://"]
        start = next((len(prefix) for prefix in prefixes if key.startswith(prefix)), 0)
        # skip over user/pass, host, port
        start = key.find("/", start)
        # insert the prefix '{' hashtag to the key as stored in redis
        key = "{" + key[start:]
        if prefix_only is False:
            key += "}"

        return key

    @classmethod
    def build_mlrun_key(cls, key):
        key = key[len("{") : -len("}")]

        return key

    def upload(self, key, src_path):
        key = RedisStore.build_redis_key(key)
        with open(src_path, "rb") as f:
            while True:
                data = f.read(1000 * 1000)
                if not data:
                    break
                self.redis.append(key, data)

    def get(self, key, size=None, offset=0):
        key = RedisStore.build_redis_key(key)
        if offset < 0:
            raise mlrun.errors.MLRunInvalidArgumentError(
                "offset argument should be >= 0"
            )
        start_offset = offset
        if size is None:
            end_offset = -1
        elif size <= 0:
            raise mlrun.errors.MLRunInvalidArgumentError("size argument should be > 0")
        else:
            end_offset = start_offset + size - 1

        return self.redis.getrange(key, start_offset, end_offset)

    def put(self, key, data, append=False):
        key = RedisStore.build_redis_key(key)
        if append:
            self.redis.append(key, data)
        else:
            self.redis.set(key, data)

    def stat(self, key):
        raise NotImplementedError()

    def listdir(self, key):
        """
        list all keys with prefix key
        """
        response = []
        key = RedisStore.build_redis_key(key, prefix_only=True)
        key += "*" if key.endswith("/") else "/*"
        for key in self.redis.scan_iter(key):
            response.append(RedisStore.build_mlrun_key(key))
        return response

    def rm(self, key, recursive=False, maxdepth=None):
        """
        delete keys, possibly recursively
        """
        if maxdepth is not None:
            raise NotImplementedError("maxdepth is not supported")

        key = RedisStore.build_redis_key(key, prefix_only=recursive)

        if recursive:
            key += "*" if key.endswith("/") else "/*"
            for k in self.redis.scan_iter(key):
                self.redis.delete(k)
            key = f"_spark:{key}"
            for k in self.redis.scan_iter(key):
                self.redis.delete(k)
        else:
            self.redis.delete(key)
