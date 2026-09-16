import os
import jwt
from fastapi import HTTPException, Security
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '..', '.env'))

SECRET_KEY = os.environ.get("JWT_SECRET", "dbpilot-dev-secret-changeme")
ALGORITHM = "HS256"

security = HTTPBearer()

def create_access_token(user_id: str, tenant_id: str) -> str:
    payload = {
        "sub": user_id,
        "tenant_id": tenant_id,
        "exp": datetime.utcnow() + timedelta(days=1)
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)

async def verify_tenant_auth(credentials: HTTPAuthorizationCredentials = Security(security)) -> str:
    """
    FastAPI Dependency: verifies the JWT and extracts tenant_id.
    The client cannot spoof this — only the backend SECRET_KEY can sign valid tokens.
    """
    token = credentials.credentials
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        tenant_id: str = payload.get("tenant_id")
        if tenant_id is None:
            raise HTTPException(status_code=401, detail="Token payload missing tenant_id")
        return tenant_id
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token has expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")
