from datetime import date

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    UploadFile,
    status,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import get_jwt_auth_manager, get_s3_storage_client
from database import get_db, UserModel, UserProfileModel
from database.models.accounts import GenderEnum
from exceptions import (
    BaseSecurityError,
    S3ConnectionError,
    S3FileUploadError,
)
from schemas.profiles import UserProfileResponseSchema
from security.interfaces import JWTAuthManagerInterface
from storages import S3StorageInterface
from validation import (
    validate_name,
    validate_image,
    validate_gender,
    validate_birth_date,
)


router = APIRouter()


def get_token(
    authorization: str | None = Header(default=None),
) -> str:
    if authorization is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization header is missing",
        )

    scheme, _, token = authorization.partition(" ")

    if scheme.lower() != "bearer" or not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=(
                "Invalid Authorization header format. "
                "Expected 'Bearer <token>'"
            ),
        )

    return token


@router.post(
    "/users/{user_id}/profile/",
    response_model=UserProfileResponseSchema,
    status_code=status.HTTP_201_CREATED,
)
async def create_profile(
    user_id: int,
    token: str = Depends(get_token),
    first_name: str = Form(...),
    last_name: str = Form(...),
    gender: str = Form(...),
    date_of_birth: str = Form(...),
    info: str = Form(...),
    avatar: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    jwt_manager: JWTAuthManagerInterface = Depends(get_jwt_auth_manager),
    s3_client: S3StorageInterface = Depends(get_s3_storage_client),
):
    try:
        payload = jwt_manager.decode_access_token(token)
    except BaseSecurityError as error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(error),
        ) from error

    token_user_id = payload.get("user_id")

    if token_user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token.",
        )

    result = await db.execute(
        select(UserModel).where(UserModel.id == token_user_id)
    )
    authenticated_user = result.scalar_one_or_none()

    if (
        authenticated_user is None
        or not authenticated_user.is_active
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found or not active.",
        )

    is_admin = authenticated_user.group_id == 3

    if token_user_id != user_id and not is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You don't have permission to edit this profile.",
        )

    result = await db.execute(
        select(UserModel).where(UserModel.id == user_id)
    )
    target_user = result.scalar_one_or_none()

    if target_user is None or not target_user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found or not active.",
        )

    result = await db.execute(
        select(UserProfileModel).where(
            UserProfileModel.user_id == user_id
        )
    )
    existing_profile = result.scalar_one_or_none()

    if existing_profile is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User already has a profile.",
        )

    try:
        validate_name(first_name)
        validate_name(last_name)
        validate_gender(gender)

        if not info.strip():
            raise ValueError(
                "Info field cannot be empty or contain only spaces."
            )

        try:
            parsed_date = date.fromisoformat(date_of_birth)
        except ValueError as error:
            raise ValueError(
                "Invalid birth date."
            ) from error

        validate_birth_date(parsed_date)
        validate_image(avatar)

    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(error),
        ) from error

    file_data = await avatar.read()

    avatar_key = f"avatars/{user_id}_avatar.jpg"

    try:
        await s3_client.upload_file(
            file_name=avatar_key,
            file_data=file_data,
        )

        avatar_url = await s3_client.get_file_url(
            avatar_key
        )

    except (S3ConnectionError, S3FileUploadError) as error:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to upload avatar. Please try again later.",
        ) from error

    profile = UserProfileModel(
        user_id=user_id,
        first_name=first_name.lower(),
        last_name=last_name.lower(),
        gender=GenderEnum(gender),
        date_of_birth=parsed_date,
        info=info,
        # IMPORTANT:
        # Tests expect the database to contain the S3 object key,
        # not the generated URL.
        avatar=avatar_key,
    )

    db.add(profile)

    try:
        await db.commit()
        await db.refresh(profile)

    except Exception:
        await db.rollback()
        raise

    return {
        "id": profile.id,
        "user_id": profile.user_id,
        "first_name": profile.first_name,
        "last_name": profile.last_name,
        "gender": profile.gender,
        "date_of_birth": profile.date_of_birth,
        "info": profile.info,
        "avatar": avatar_url,
    }
