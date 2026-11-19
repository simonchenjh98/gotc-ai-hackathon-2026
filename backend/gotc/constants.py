"""Competition and session constants shared by the event service."""

DIMENSIONS = [
    {"id": "problem", "name": "问题价值与理解", "max": 20},
    {"id": "ai", "name": "方案与AI使用", "max": 20},
    {"id": "prototype", "name": "原型完成度", "max": 20},
    {"id": "testing", "name": "测试与改进", "max": 15},
    {"id": "opensource", "name": "开源与责任", "max": 15},
    {"id": "teamwork", "name": "团队协作与表达", "max": 10},
]
SESSION_SECONDS = 12 * 3600
# Bound password-check CPU/memory without holding the database writer lock.
LOGIN_KDF_CONCURRENCY = 2
VOTE_LIMIT = 3
COOKIE_NAME = "gotc_session"
