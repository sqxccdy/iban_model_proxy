"""Environment and project key registry stored in Redis."""

import json
import re
from hashlib import sha256

AUTHORIZATION_MAPPING_KEY = "monitor:authorization:mappings"
PROJECT_MAPPING_KEY = "monitor:project:mappings"
PROJECT_OWNER_KEY = "monitor:project:owners"


REGISTRY_SCRIPT = """
local action, key, value, parent = ARGV[1], ARGV[2], ARGV[3], ARGV[4]
local function is_environment(candidate)
    return redis.call('HEXISTS', KEYS[1], candidate) == 1 or
           redis.call('HEXISTS', KEYS[1], 'Bearer ' .. candidate) == 1
end
if action == 'save_environment' then
    if redis.call('HEXISTS', KEYS[2], key) == 1 or redis.call('HEXISTS', KEYS[3], key) == 1 then
        return 'project_key'
    end
    redis.call('HSET', KEYS[1], key, value)
    redis.call('HDEL', KEYS[1], 'Bearer ' .. key)
elseif action == 'delete_environment' then
    for _, raw in ipairs(redis.call('HVALS', KEYS[2])) do
        local ok, project = pcall(cjson.decode, raw)
        if ok and type(project) == 'table' and project.environment_authorization == key then
            return 'has_projects'
        end
    end
    redis.call('HDEL', KEYS[1], key, 'Bearer ' .. key)
elseif action == 'save_project' then
    if not is_environment(parent) then return 'missing_environment' end
    if is_environment(key) then return 'environment_key' end
    local owner = redis.call('HGET', KEYS[3], key)
    if owner and owner ~= parent then return 'different_environment' end
    redis.call('HSET', KEYS[2], key, value)
    redis.call('HSET', KEYS[3], key, parent)
elseif action == 'delete_project' then
    redis.call('HDEL', KEYS[2], key)
else
    return 'invalid_action'
end
return 'ok'
"""


async def mutate_registry(redis, action, authorization, value="", parent=""):
    return await redis.eval(REGISTRY_SCRIPT, 3, AUTHORIZATION_MAPPING_KEY,
                            PROJECT_MAPPING_KEY, PROJECT_OWNER_KEY,
                            action, authorization, value, parent)


def normalize_authorization(authorization: str | None) -> str | None:
    if not isinstance(authorization, str):
        return None
    value = authorization.strip()
    if value.lower() == "bearer":
        return None
    bearer = re.match(r"(?i)^bearer[ \t]+(.*)$", value)
    if bearer:
        value = bearer.group(1).strip()
    return value or None


def authorization_stats_id(authorization: str) -> str:
    return sha256(authorization.encode("utf-8")).hexdigest()


async def get_authorization_mappings(redis) -> list[dict[str, str]]:
    mappings = await redis.hgetall(AUTHORIZATION_MAPPING_KEY)
    normalized = {
        normalize_authorization(authorization): description
        for authorization, description in mappings.items()
        if normalize_authorization(authorization)
    }
    # Canonical entries take precedence over any previously saved Bearer-prefixed entries.
    normalized.update({authorization: description for authorization, description in mappings.items()
                       if authorization == normalize_authorization(authorization)})
    return [
        {"authorization": authorization, "description": description}
        for authorization, description in sorted(normalized.items())
    ]


async def get_authorization_description(redis, authorization: str | None) -> str | None:
    authorization = normalize_authorization(authorization)
    if not authorization:
        return None
    description, legacy_description = await redis.hmget(
        AUTHORIZATION_MAPPING_KEY, authorization, f"Bearer {authorization}"
    )
    return description or legacy_description


async def get_project_mappings(redis, environment_keys=None):
    raw = await redis.hgetall(PROJECT_MAPPING_KEY)
    projects = []
    for authorization, value in raw.items():
        try:
            project = json.loads(value)
        except (TypeError, ValueError):
            continue
        if not isinstance(project, dict):
            continue
        environment = normalize_authorization(project.get("environment_authorization"))
        if not isinstance(project.get("name"), str) or not environment:
            continue
        if environment_keys is not None and environment not in environment_keys:
            continue
        projects.append({"authorization": authorization, "name": project["name"],
                         "environment_authorization": environment})
    return sorted(projects, key=lambda item: (item["environment_authorization"], item["name"]))


async def get_authorization_context(redis, authorization):
    authorization = normalize_authorization(authorization)
    if not authorization:
        return None, None
    async with redis.pipeline() as pipe:
        pipe.hmget(AUTHORIZATION_MAPPING_KEY, authorization, f"Bearer {authorization}")
        pipe.hget(PROJECT_MAPPING_KEY, authorization)
        descriptions, raw_project = await pipe.execute()
    if description := next((value for value in descriptions if value), None):
        return description, {"authorization": authorization, "environment_authorization": authorization}
    if raw_project:
        try:
            project = json.loads(raw_project)
            environment = normalize_authorization(project["environment_authorization"])
            if environment and isinstance(project.get("name"), str):
                return project["name"], {"authorization": authorization,
                                          "environment_authorization": environment, "project": True}
        except (TypeError, ValueError, KeyError):
            pass
    return None, None
