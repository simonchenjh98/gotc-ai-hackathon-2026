"""HTTP errors shared by the event service and judging workflow."""


class APIError(Exception):
    def __init__(self, status: int, message: str, code: str | None = None) -> None:
        self.status = status
        self.message = message
        self.code = code
