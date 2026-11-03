"""HTTP errors shared by the event service and judging workflow."""


class APIError(Exception):
    def __init__(self, status, message, code=None):
        self.status,self.message,self.code=status,message,code
