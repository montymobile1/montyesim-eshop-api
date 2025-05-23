from typing import Any


class CustomException(Exception):
    def __init__(self, name: str, details: str, code: int):
        self.name = name
        self.details = details
        self.code = code
        super().__init__(f"{name}: {details}")


class EsimHubException(CustomException):
    def __init__(self, details: str | dict | Any):
        self.name = "ESIMHub Exception"
        self.details = details
        self.code = 400
        if isinstance(details, dict):
            self.details = details["message"] or details["code"]
        else:
            self.details = str(details)
        super().__init__(name=self.name, details=self.details, code=self.code)


class BadRequestException(CustomException):
    def __init__(self, details: str):
        super().__init__(code=400, name="BadRequest Exception", details=details)


class DatabaseException(CustomException):
    def __init__(self, details: str):
        super().__init__(code=400, name="DataException", details=details)

class DCBException(CustomException):
    def __init__(self, details: str | dict | Any):
        self.name = "DCB Exception"
        self.details = details
        self.code = 400
        if isinstance(details, dict):
            self.details = details["message"] or details["code"]
        else:
            self.details = str(details)
        super().__init__(name=self.name, details=self.details, code=self.code)