from typing import Optional, Generic, TypeVar

from pydantic import BaseModel, ConfigDict

T = TypeVar('T')


class Response(BaseModel, Generic[T]):
    status: str
    totalCount: int
    data: Optional[T] = None
    title: Optional[str]
    message: Optional[str]
    developerMessage: Optional[str]
    responseCode: int

    model_config = ConfigDict(from_attributes=True, extra="ignore")


class ResponseHelper:
    @staticmethod
    def success_response(message: str = None) -> Response[None]:
        return Response(status='success', totalCount=0, data=None, title=None, message=message, developerMessage=None,
                        responseCode=200)

    @staticmethod
    def success_data_response(data: T, total_count: int, message: str = None) -> Response[T]:
        return Response(status='success', totalCount=total_count, data=data, title="Success", message=message,
                        developerMessage=None,
                        responseCode=200)

    @staticmethod
    def success_data_response_with_message(data: T, message: str, total_count: int) -> Response[T]:
        return Response(status='success', totalCount=total_count, data=data, title="Success", message=message,
                        developerMessage=None,
                        responseCode=200)

    @staticmethod
    def error_response(status_code: int, error: str, title: str = None,
                       developer_message: str = None) -> Response[None]:
        return Response(status='failed', totalCount=0, data=None, title=title, message=error,
                        developerMessage=developer_message,
                        responseCode=status_code)
