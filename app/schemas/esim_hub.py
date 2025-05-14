from typing import Optional, List

from pydantic import BaseModel, ConfigDict


class EsimHubOrderResponse(BaseModel):
    orderId: str
    totalAmount: float
    title: str
    referenceOrderId: Optional[str] = None
    createdDate: str
    uniqueIdentifier: str
    displaySubTitle: str
    price: float
    quantity: int
    bundleCode: Optional[str] = None
    bundleGuid: str
    allowTopup: Optional[bool] = None
    orderStatus: str
    qrCode: Optional[str] = None
    activationCode: Optional[str] = None
    smdpAdress: Optional[str] = None
    validityData: Optional[str] = None
    iccid: Optional[str] = None


class GlobalConfigurationResponse(BaseModel):
    key: str
    value: str

    model_config = ConfigDict(from_attributes=True, extra="ignore")


class ContentDetailsResponse(BaseModel):
    name: str
    description: str
    languageCode: str


class ContentCategoryResponse(BaseModel):
    tag: str
    contentCategoryDetails: List[ContentDetailsResponse]


class ContentChildrenResponse(BaseModel):
    contentDetails: List[ContentDetailsResponse]


class ContentResponse(BaseModel):
    tag: str
    contentDetails: List[ContentDetailsResponse]
    contentCategory: ContentCategoryResponse
    children: Optional[List[ContentChildrenResponse]] = []

    model_config = ConfigDict(from_attributes=True, extra="ignore")
