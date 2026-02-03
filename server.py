from fastapi import FastAPI, APIRouter, HTTPException, Depends, BackgroundTasks, Response, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.responses import RedirectResponse
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
import os
import logging
from pathlib import Path
from pydantic import BaseModel, Field, EmailStr, ConfigDict
from typing import List, Optional, Dict, Any
import uuid
from datetime import datetime, timezone, timedelta
import bcrypt
import jwt
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import base64
import asyncio
import re
from urllib.parse import urlencode, quote

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

# MongoDB connection
mongo_url = os.environ['MONGO_URL']
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ['DB_NAME']]

# JWT Configuration
JWT_SECRET = os.environ.get('JWT_SECRET', 'your-secret-key-change-in-production')
JWT_ALGORITHM = "HS256"
JWT_EXPIRATION_HOURS = 24

# SMTP Configuration
SMTP_HOST = os.environ.get('SMTP_HOST', '')
SMTP_PORT = int(os.environ.get('SMTP_PORT', 587))
SMTP_USER = os.environ.get('SMTP_USER', '')
SMTP_PASSWORD = os.environ.get('SMTP_PASSWORD', '')
SMTP_FROM_EMAIL = os.environ.get('SMTP_FROM_EMAIL', '')
SMTP_USE_SSL = os.environ.get('SMTP_USE_SSL', 'false').lower() == 'true'
WELCOME_EMAIL_ENABLED = os.environ.get('WELCOME_EMAIL_ENABLED', 'false').lower() == 'true'

# App URL for tracking
APP_URL = os.environ.get('APP_URL', os.environ.get('REACT_APP_BACKEND_URL', 'http://localhost:8001'))

# Create the main app
app = FastAPI(title="EmailBlast - Email Marketing Platform")

from starlette.middleware.cors import CORSMiddleware

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://zerocostsites.com",
        "https://www.zerocostsites.com",
        "http://localhost:3000",
        "http://localhost:5173"
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Create a router with the /api prefix
api_router = APIRouter(prefix="/api")

# Security
security = HTTPBearer()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


# ==================== MODELS ====================

# Auth Models
class UserCreate(BaseModel):
    name: str
    email: EmailStr
    password: str

class UserLogin(BaseModel):
    email: EmailStr
    password: str

class UserResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str
    name: str
    email: str
    created_at: str

class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserResponse

# Tag Models
class TagCreate(BaseModel):
    name: str
    color: str = "#047857"

class TagResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str
    name: str
    color: str
    user_id: str
    created_at: str

# Subscriber Models
class SubscriberCreate(BaseModel):
    name: str
    email: EmailStr
    tags: List[str] = []

class SubscriberUpdate(BaseModel):
    name: Optional[str] = None
    tags: Optional[List[str]] = None

class SubscriberResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str
    name: str
    email: str
    user_id: str
    created_at: str
    status: str = "active"
    tags: List[str] = []
    opens_count: int = 0
    clicks_count: int = 0
    last_activity: Optional[str] = None

# Segment Models
class SegmentFilter(BaseModel):
    field: str  # tags, signup_date, opens_count, clicks_count
    operator: str  # contains, not_contains, gt, lt, eq, between
    value: Any

class SegmentCreate(BaseModel):
    name: str
    filters: List[SegmentFilter]
    match_type: str = "all"  # all or any

class SegmentResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str
    name: str
    filters: List[Dict]
    match_type: str
    user_id: str
    created_at: str
    subscriber_count: int = 0

# Campaign Models
class CampaignCreate(BaseModel):
    subject: str
    body: str
    scheduled_at: Optional[str] = None
    segment_id: Optional[str] = None
    tag_filter: Optional[List[str]] = None

class CampaignResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str
    subject: str
    body: str
    user_id: str
    created_at: str
    scheduled_at: Optional[str] = None
    status: str = "draft"
    sent_count: int = 0
    segment_id: Optional[str] = None
    tag_filter: Optional[List[str]] = None
    opens_count: int = 0
    clicks_count: int = 0
    unique_opens: int = 0
    unique_clicks: int = 0

# Workflow Models
class WorkflowStepCreate(BaseModel):
    step_order: int
    subject: str
    body: str
    delay_days: int = 0
    delay_hours: int = 0

class WorkflowCreate(BaseModel):
    name: str
    trigger_type: str  # new_subscriber, email_opened, link_clicked
    trigger_config: Dict = {}
    steps: List[WorkflowStepCreate]

class WorkflowResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str
    name: str
    trigger_type: str
    trigger_config: Dict
    user_id: str
    created_at: str
    status: str = "active"
    steps: List[Dict] = []
    enrolled_count: int = 0
    completed_count: int = 0

# Analytics Models
class CampaignAnalytics(BaseModel):
    campaign_id: str
    subject: str
    sent_count: int
    opens_count: int
    unique_opens: int
    clicks_count: int
    unique_clicks: int
    open_rate: float
    click_rate: float

class DashboardStats(BaseModel):
    total_subscribers: int
    total_campaigns: int
    sent_campaigns: int
    draft_campaigns: int
    total_tags: int
    total_segments: int
    total_workflows: int
    active_workflows: int
    total_opens: int
    total_clicks: int

class MessageResponse(BaseModel):
    message: str


# ==================== HELPER FUNCTIONS ====================

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')

def verify_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode('utf-8'), hashed.encode('utf-8'))

def create_token(user_id: str, email: str) -> str:
    payload = {
        "user_id": user_id,
        "email": email,
        "exp": datetime.now(timezone.utc) + timedelta(hours=JWT_EXPIRATION_HOURS)
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)

async def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)):
    try:
        token = credentials.credentials
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        user_id = payload.get("user_id")
        if not user_id:
            raise HTTPException(status_code=401, detail="Invalid token")
        
        user = await db.users.find_one({"id": user_id}, {"_id": 0, "password": 0})
        if not user:
            raise HTTPException(status_code=401, detail="User not found")
        return user
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")


def generate_tracking_id() -> str:
    return str(uuid.uuid4())


def add_tracking_to_email(body: str, campaign_id: str, subscriber_id: str, user_id: str) -> str:
    """Add tracking pixel and convert links to tracked links"""
    tracking_id = generate_tracking_id()
    
    # Add tracking pixel at the end of email
    tracking_pixel = f'<img src="{APP_URL}/api/track/open/{campaign_id}/{subscriber_id}/{tracking_id}" width="1" height="1" style="display:none;" />'
    
    # Convert links to tracked links
    def replace_link(match):
        original_url = match.group(1)
        if 'track/click' in original_url or 'track/open' in original_url:
            return match.group(0)
        encoded_url = quote(original_url, safe='')
        tracked_url = f'{APP_URL}/api/track/click/{campaign_id}/{subscriber_id}?url={encoded_url}'
        return f'href="{tracked_url}"'
    
    tracked_body = re.sub(r'href="([^"]+)"', replace_link, body)
    tracked_body = re.sub(r"href='([^']+)'", replace_link, tracked_body)
    
    # Add pixel before closing body tag or at end
    if '</body>' in tracked_body.lower():
        tracked_body = re.sub(r'(</body>)', f'{tracking_pixel}\\1', tracked_body, flags=re.IGNORECASE)
    else:
        tracked_body += tracking_pixel
    
    return tracked_body


def send_email_smtp(to_email: str, subject: str, body: str, is_html: bool = True):
    """Send email using SMTP"""
    if not all([SMTP_HOST, SMTP_USER, SMTP_PASSWORD, SMTP_FROM_EMAIL]):
        logger.warning("SMTP not configured. Email not sent.")
        return False
    
    try:
        msg = MIMEMultipart('alternative')
        msg['Subject'] = subject
        msg['From'] = SMTP_FROM_EMAIL
        msg['To'] = to_email
        
        if is_html:
            part = MIMEText(body, 'html')
        else:
            part = MIMEText(body, 'plain')
        msg.attach(part)
        
        if SMTP_USE_SSL:
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as server:
                server.login(SMTP_USER, SMTP_PASSWORD)
                server.sendmail(SMTP_FROM_EMAIL, to_email, msg.as_string())
        else:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
                server.starttls()
                server.login(SMTP_USER, SMTP_PASSWORD)
                server.sendmail(SMTP_FROM_EMAIL, to_email, msg.as_string())
        
        logger.info(f"Email sent to {to_email}")
        return True
    except Exception as e:
        logger.error(f"Failed to send email to {to_email}: {str(e)}")
        return False


async def send_welcome_email(subscriber_name: str, subscriber_email: str, user_id: str, subscriber_id: str):
    """Send welcome email and trigger workflows"""
    if WELCOME_EMAIL_ENABLED:
        subject = "Welcome to our Newsletter!"
        body = f"""
        <html>
        <body style="font-family: Inter, sans-serif; padding: 20px; background-color: #f8fafc;">
            <div style="max-width: 600px; margin: 0 auto; background-color: white; padding: 40px; border-radius: 8px; box-shadow: 0 2px 8px rgba(0,0,0,0.04);">
                <h1 style="color: #047857; font-family: Manrope, sans-serif;">Welcome, {subscriber_name}!</h1>
                <p style="color: #64748b; line-height: 1.6;">
                    Thank you for subscribing to our newsletter. We're excited to have you on board!
                </p>
            </div>
        </body>
        </html>
        """
        send_email_smtp(subscriber_email, subject, body)
    
    # Trigger new_subscriber workflows
    await trigger_workflow_event(user_id, "new_subscriber", subscriber_id)


async def trigger_workflow_event(user_id: str, event_type: str, subscriber_id: str, extra_data: Dict = None):
    """Trigger workflows based on events"""
    workflows = await db.workflows.find({
        "user_id": user_id,
        "trigger_type": event_type,
        "status": "active"
    }, {"_id": 0}).to_list(100)
    
    for workflow in workflows:
        # Check if subscriber is already enrolled
        existing = await db.workflow_subscribers.find_one({
            "workflow_id": workflow['id'],
            "subscriber_id": subscriber_id
        })
        
        if not existing:
            # Enroll subscriber in workflow
            enrollment = {
                "id": str(uuid.uuid4()),
                "workflow_id": workflow['id'],
                "subscriber_id": subscriber_id,
                "user_id": user_id,
                "current_step": 0,
                "status": "active",
                "enrolled_at": datetime.now(timezone.utc).isoformat(),
                "next_email_at": datetime.now(timezone.utc).isoformat(),
                "completed_steps": []
            }
            await db.workflow_subscribers.insert_one(enrollment)
            
            # Update workflow enrolled count
            await db.workflows.update_one(
                {"id": workflow['id']},
                {"$inc": {"enrolled_count": 1}}
            )


async def process_workflow_emails():
    """Process pending workflow emails (called periodically)"""
    now = datetime.now(timezone.utc)
    
    # Find all active workflow subscriptions that need processing
    pending = await db.workflow_subscribers.find({
        "status": "active",
        "next_email_at": {"$lte": now.isoformat()}
    }, {"_id": 0}).to_list(1000)
    
    for enrollment in pending:
        workflow = await db.workflows.find_one({"id": enrollment['workflow_id']}, {"_id": 0})
        if not workflow or workflow['status'] != 'active':
            continue
        
        subscriber = await db.subscribers.find_one({"id": enrollment['subscriber_id']}, {"_id": 0})
        if not subscriber or subscriber['status'] != 'active':
            continue
        
        steps = workflow.get('steps', [])
        current_step = enrollment['current_step']
        
        if current_step >= len(steps):
            # Workflow completed
            await db.workflow_subscribers.update_one(
                {"id": enrollment['id']},
                {"$set": {"status": "completed"}}
            )
            await db.workflows.update_one(
                {"id": workflow['id']},
                {"$inc": {"completed_count": 1}}
            )
            continue
        
        step = steps[current_step]
        
        # Send email with tracking
        personalized_body = step['body'].replace('{{name}}', subscriber['name'])
        tracked_body = add_tracking_to_email(
            personalized_body,
            f"workflow_{workflow['id']}_{current_step}",
            subscriber['id'],
            enrollment['user_id']
        )
        
        send_email_smtp(subscriber['email'], step['subject'], tracked_body)
        
        # Calculate next email time
        next_step = current_step + 1
        if next_step < len(steps):
            next_step_data = steps[next_step]
            delay = timedelta(days=next_step_data.get('delay_days', 0), hours=next_step_data.get('delay_hours', 0))
            next_email_at = now + delay
        else:
            next_email_at = now  # Will be marked completed on next run
        
        # Update enrollment
        await db.workflow_subscribers.update_one(
            {"id": enrollment['id']},
            {
                "$set": {
                    "current_step": next_step,
                    "next_email_at": next_email_at.isoformat()
                },
                "$push": {
                    "completed_steps": {
                        "step": current_step,
                        "sent_at": now.isoformat()
                    }
                }
            }
        )


async def send_campaign_emails(campaign_id: str, user_id: str):
    """Send campaign to subscribers (filtered by segment/tags)"""
    campaign = await db.campaigns.find_one({"id": campaign_id, "user_id": user_id}, {"_id": 0})
    if not campaign:
        logger.error(f"Campaign {campaign_id} not found")
        return
    
    # Build subscriber query
    query = {"user_id": user_id, "status": "active"}
    
    # Apply tag filter
    if campaign.get('tag_filter') and len(campaign['tag_filter']) > 0:
        query['tags'] = {"$in": campaign['tag_filter']}
    
    # Apply segment filter
    if campaign.get('segment_id'):
        segment = await db.segments.find_one({"id": campaign['segment_id']}, {"_id": 0})
        if segment:
            segment_query = await build_segment_query(segment, user_id)
            query.update(segment_query)
    
    subscribers = await db.subscribers.find(query, {"_id": 0}).to_list(10000)
    
    sent_count = 0
    for subscriber in subscribers:
        # Personalize and add tracking
        personalized_body = campaign['body'].replace('{{name}}', subscriber['name'])
        tracked_body = add_tracking_to_email(personalized_body, campaign_id, subscriber['id'], user_id)
        
        if send_email_smtp(subscriber['email'], campaign['subject'], tracked_body):
            sent_count += 1
    
    # Update campaign status
    await db.campaigns.update_one(
        {"id": campaign_id},
        {"$set": {"status": "sent", "sent_count": sent_count, "sent_at": datetime.now(timezone.utc).isoformat()}}
    )
    logger.info(f"Campaign {campaign_id} sent to {sent_count} subscribers")


async def build_segment_query(segment: Dict, user_id: str) -> Dict:
    """Build MongoDB query from segment filters"""
    conditions = []
    
    for f in segment.get('filters', []):
        field = f['field']
        op = f['operator']
        value = f['value']
        
        if field == 'tags':
            if op == 'contains':
                conditions.append({"tags": {"$in": [value] if isinstance(value, str) else value}})
            elif op == 'not_contains':
                conditions.append({"tags": {"$nin": [value] if isinstance(value, str) else value}})
        elif field == 'signup_date':
            if op == 'gt':
                conditions.append({"created_at": {"$gt": value}})
            elif op == 'lt':
                conditions.append({"created_at": {"$lt": value}})
            elif op == 'between' and isinstance(value, list) and len(value) == 2:
                conditions.append({"created_at": {"$gte": value[0], "$lte": value[1]}})
        elif field in ['opens_count', 'clicks_count']:
            if op == 'gt':
                conditions.append({field: {"$gt": int(value)}})
            elif op == 'lt':
                conditions.append({field: {"$lt": int(value)}})
            elif op == 'eq':
                conditions.append({field: int(value)})
    
    if not conditions:
        return {}
    
    if segment.get('match_type') == 'any':
        return {"$or": conditions}
    return {"$and": conditions}


# ==================== AUTH ROUTES ====================

@api_router.post("/auth/register", response_model=TokenResponse)
async def register(user_data: UserCreate):
    existing = await db.users.find_one({"email": user_data.email})
    if existing:
        raise HTTPException(status_code=400, detail="Email already registered")
    
    user_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    
    user_doc = {
        "id": user_id,
        "name": user_data.name,
        "email": user_data.email,
        "password": hash_password(user_data.password),
        "created_at": now
    }
    
    await db.users.insert_one(user_doc)
    
    token = create_token(user_id, user_data.email)
    user_response = UserResponse(id=user_id, name=user_data.name, email=user_data.email, created_at=now)
    
    return TokenResponse(access_token=token, user=user_response)


@api_router.post("/auth/login", response_model=TokenResponse)
async def login(credentials: UserLogin):
    user = await db.users.find_one({"email": credentials.email}, {"_id": 0})
    if not user:
        raise HTTPException(status_code=401, detail="Invalid email or password")
    
    if not verify_password(credentials.password, user['password']):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    
    token = create_token(user['id'], user['email'])
    user_response = UserResponse(id=user['id'], name=user['name'], email=user['email'], created_at=user['created_at'])
    
    return TokenResponse(access_token=token, user=user_response)


@api_router.get("/auth/me", response_model=UserResponse)
async def get_me(current_user: dict = Depends(get_current_user)):
    return UserResponse(**current_user)


# ==================== TAG ROUTES ====================

@api_router.post("/tags", response_model=TagResponse)
async def create_tag(tag_data: TagCreate, current_user: dict = Depends(get_current_user)):
    # Check if tag exists
    existing = await db.tags.find_one({"name": tag_data.name, "user_id": current_user['id']})
    if existing:
        raise HTTPException(status_code=400, detail="Tag already exists")
    
    tag_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    
    tag_doc = {
        "id": tag_id,
        "name": tag_data.name,
        "color": tag_data.color,
        "user_id": current_user['id'],
        "created_at": now
    }
    
    await db.tags.insert_one(tag_doc)
    return TagResponse(**tag_doc)


@api_router.get("/tags", response_model=List[TagResponse])
async def get_tags(current_user: dict = Depends(get_current_user)):
    tags = await db.tags.find({"user_id": current_user['id']}, {"_id": 0}).to_list(1000)
    return [TagResponse(**t) for t in tags]


@api_router.delete("/tags/{tag_id}", response_model=MessageResponse)
async def delete_tag(tag_id: str, current_user: dict = Depends(get_current_user)):
    result = await db.tags.delete_one({"id": tag_id, "user_id": current_user['id']})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Tag not found")
    
    # Remove tag from subscribers
    await db.subscribers.update_many(
        {"user_id": current_user['id'], "tags": tag_id},
        {"$pull": {"tags": tag_id}}
    )
    
    return MessageResponse(message="Tag deleted successfully")


# ==================== SUBSCRIBER ROUTES ====================

@api_router.post("/subscribers", response_model=SubscriberResponse)
async def create_subscriber(
    subscriber_data: SubscriberCreate,
    background_tasks: BackgroundTasks,
    current_user: dict = Depends(get_current_user)
):
    existing = await db.subscribers.find_one({
        "email": subscriber_data.email,
        "user_id": current_user['id']
    })
    if existing:
        raise HTTPException(status_code=400, detail="Subscriber already exists")
    
    subscriber_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    
    subscriber_doc = {
        "id": subscriber_id,
        "name": subscriber_data.name,
        "email": subscriber_data.email,
        "user_id": current_user['id'],
        "created_at": now,
        "status": "active",
        "tags": subscriber_data.tags,
        "opens_count": 0,
        "clicks_count": 0,
        "last_activity": None
    }
    
    await db.subscribers.insert_one(subscriber_doc)
    
    # Send welcome email and trigger workflows
    background_tasks.add_task(send_welcome_email, subscriber_data.name, subscriber_data.email, current_user['id'], subscriber_id)
    
    return SubscriberResponse(**subscriber_doc)


@api_router.get("/subscribers", response_model=List[SubscriberResponse])
async def get_subscribers(
    tag: Optional[str] = None,
    segment_id: Optional[str] = None,
    current_user: dict = Depends(get_current_user)
):
    query = {"user_id": current_user['id']}
    
    if tag:
        query['tags'] = tag
    
    if segment_id:
        segment = await db.segments.find_one({"id": segment_id, "user_id": current_user['id']}, {"_id": 0})
        if segment:
            segment_query = await build_segment_query(segment, current_user['id'])
            query.update(segment_query)
    
    subscribers = await db.subscribers.find(query, {"_id": 0}).sort("created_at", -1).to_list(10000)
    return [SubscriberResponse(**s) for s in subscribers]


@api_router.put("/subscribers/{subscriber_id}", response_model=SubscriberResponse)
async def update_subscriber(
    subscriber_id: str,
    update_data: SubscriberUpdate,
    current_user: dict = Depends(get_current_user)
):
    update_dict = {k: v for k, v in update_data.model_dump().items() if v is not None}
    if not update_dict:
        raise HTTPException(status_code=400, detail="No data to update")
    
    result = await db.subscribers.update_one(
        {"id": subscriber_id, "user_id": current_user['id']},
        {"$set": update_dict}
    )
    
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Subscriber not found")
    
    subscriber = await db.subscribers.find_one({"id": subscriber_id}, {"_id": 0})
    return SubscriberResponse(**subscriber)


@api_router.delete("/subscribers/{subscriber_id}", response_model=MessageResponse)
async def delete_subscriber(subscriber_id: str, current_user: dict = Depends(get_current_user)):
    result = await db.subscribers.delete_one({"id": subscriber_id, "user_id": current_user['id']})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Subscriber not found")
    return MessageResponse(message="Subscriber deleted successfully")


# ==================== SEGMENT ROUTES ====================

@api_router.post("/segments", response_model=SegmentResponse)
async def create_segment(segment_data: SegmentCreate, current_user: dict = Depends(get_current_user)):
    segment_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    
    segment_doc = {
        "id": segment_id,
        "name": segment_data.name,
        "filters": [f.model_dump() for f in segment_data.filters],
        "match_type": segment_data.match_type,
        "user_id": current_user['id'],
        "created_at": now
    }
    
    await db.segments.insert_one(segment_doc)
    
    # Calculate subscriber count
    query = {"user_id": current_user['id']}
    segment_query = await build_segment_query(segment_doc, current_user['id'])
    query.update(segment_query)
    count = await db.subscribers.count_documents(query)
    
    return SegmentResponse(**segment_doc, subscriber_count=count)


@api_router.get("/segments", response_model=List[SegmentResponse])
async def get_segments(current_user: dict = Depends(get_current_user)):
    segments = await db.segments.find({"user_id": current_user['id']}, {"_id": 0}).to_list(100)
    
    result = []
    for seg in segments:
        query = {"user_id": current_user['id']}
        segment_query = await build_segment_query(seg, current_user['id'])
        query.update(segment_query)
        count = await db.subscribers.count_documents(query)
        result.append(SegmentResponse(**seg, subscriber_count=count))
    
    return result


@api_router.delete("/segments/{segment_id}", response_model=MessageResponse)
async def delete_segment(segment_id: str, current_user: dict = Depends(get_current_user)):
    result = await db.segments.delete_one({"id": segment_id, "user_id": current_user['id']})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Segment not found")
    return MessageResponse(message="Segment deleted successfully")


# ==================== CAMPAIGN ROUTES ====================

@api_router.post("/campaigns", response_model=CampaignResponse)
async def create_campaign(campaign_data: CampaignCreate, current_user: dict = Depends(get_current_user)):
    campaign_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    
    campaign_doc = {
        "id": campaign_id,
        "subject": campaign_data.subject,
        "body": campaign_data.body,
        "user_id": current_user['id'],
        "created_at": now,
        "scheduled_at": campaign_data.scheduled_at,
        "status": "draft",
        "sent_count": 0,
        "segment_id": campaign_data.segment_id,
        "tag_filter": campaign_data.tag_filter,
        "opens_count": 0,
        "clicks_count": 0,
        "unique_opens": 0,
        "unique_clicks": 0
    }
    
    await db.campaigns.insert_one(campaign_doc)
    return CampaignResponse(**campaign_doc)


@api_router.get("/campaigns", response_model=List[CampaignResponse])
async def get_campaigns(current_user: dict = Depends(get_current_user)):
    campaigns = await db.campaigns.find({"user_id": current_user['id']}, {"_id": 0}).sort("created_at", -1).to_list(1000)
    return [CampaignResponse(**c) for c in campaigns]


@api_router.get("/campaigns/{campaign_id}", response_model=CampaignResponse)
async def get_campaign(campaign_id: str, current_user: dict = Depends(get_current_user)):
    campaign = await db.campaigns.find_one({"id": campaign_id, "user_id": current_user['id']}, {"_id": 0})
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    return CampaignResponse(**campaign)


@api_router.get("/campaigns/{campaign_id}/analytics", response_model=CampaignAnalytics)
async def get_campaign_analytics(campaign_id: str, current_user: dict = Depends(get_current_user)):
    campaign = await db.campaigns.find_one({"id": campaign_id, "user_id": current_user['id']}, {"_id": 0})
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    
    sent = campaign.get('sent_count', 0)
    opens = campaign.get('opens_count', 0)
    unique_opens = campaign.get('unique_opens', 0)
    clicks = campaign.get('clicks_count', 0)
    unique_clicks = campaign.get('unique_clicks', 0)
    
    open_rate = (unique_opens / sent * 100) if sent > 0 else 0
    click_rate = (unique_clicks / sent * 100) if sent > 0 else 0
    
    return CampaignAnalytics(
        campaign_id=campaign_id,
        subject=campaign['subject'],
        sent_count=sent,
        opens_count=opens,
        unique_opens=unique_opens,
        clicks_count=clicks,
        unique_clicks=unique_clicks,
        open_rate=round(open_rate, 2),
        click_rate=round(click_rate, 2)
    )


@api_router.delete("/campaigns/{campaign_id}", response_model=MessageResponse)
async def delete_campaign(campaign_id: str, current_user: dict = Depends(get_current_user)):
    result = await db.campaigns.delete_one({"id": campaign_id, "user_id": current_user['id']})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Campaign not found")
    return MessageResponse(message="Campaign deleted successfully")


@api_router.post("/campaigns/{campaign_id}/send", response_model=MessageResponse)
async def send_campaign(
    campaign_id: str,
    background_tasks: BackgroundTasks,
    current_user: dict = Depends(get_current_user)
):
    campaign = await db.campaigns.find_one({"id": campaign_id, "user_id": current_user['id']}, {"_id": 0})
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    
    if campaign['status'] == 'sent':
        raise HTTPException(status_code=400, detail="Campaign already sent")
    
    if not all([SMTP_HOST, SMTP_USER, SMTP_PASSWORD, SMTP_FROM_EMAIL]):
        raise HTTPException(status_code=400, detail="SMTP not configured")
    
    # Build query to count recipients
    query = {"user_id": current_user['id'], "status": "active"}
    if campaign.get('tag_filter') and len(campaign['tag_filter']) > 0:
        query['tags'] = {"$in": campaign['tag_filter']}
    if campaign.get('segment_id'):
        segment = await db.segments.find_one({"id": campaign['segment_id']}, {"_id": 0})
        if segment:
            segment_query = await build_segment_query(segment, current_user['id'])
            query.update(segment_query)
    
    subscriber_count = await db.subscribers.count_documents(query)
    
    if subscriber_count == 0:
        raise HTTPException(status_code=400, detail="No subscribers match the criteria")
    
    await db.campaigns.update_one({"id": campaign_id}, {"$set": {"status": "sending"}})
    background_tasks.add_task(send_campaign_emails, campaign_id, current_user['id'])
    
    return MessageResponse(message=f"Campaign is being sent to {subscriber_count} subscribers")


# ==================== WORKFLOW ROUTES ====================

@api_router.post("/workflows", response_model=WorkflowResponse)
async def create_workflow(workflow_data: WorkflowCreate, current_user: dict = Depends(get_current_user)):
    workflow_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    
    steps = []
    for step in sorted(workflow_data.steps, key=lambda x: x.step_order):
        steps.append({
            "step_order": step.step_order,
            "subject": step.subject,
            "body": step.body,
            "delay_days": step.delay_days,
            "delay_hours": step.delay_hours
        })
    
    workflow_doc = {
        "id": workflow_id,
        "name": workflow_data.name,
        "trigger_type": workflow_data.trigger_type,
        "trigger_config": workflow_data.trigger_config,
        "user_id": current_user['id'],
        "created_at": now,
        "status": "active",
        "steps": steps,
        "enrolled_count": 0,
        "completed_count": 0
    }
    
    await db.workflows.insert_one(workflow_doc)
    return WorkflowResponse(**workflow_doc)


@api_router.get("/workflows", response_model=List[WorkflowResponse])
async def get_workflows(current_user: dict = Depends(get_current_user)):
    workflows = await db.workflows.find({"user_id": current_user['id']}, {"_id": 0}).sort("created_at", -1).to_list(100)
    return [WorkflowResponse(**w) for w in workflows]


@api_router.get("/workflows/{workflow_id}", response_model=WorkflowResponse)
async def get_workflow(workflow_id: str, current_user: dict = Depends(get_current_user)):
    workflow = await db.workflows.find_one({"id": workflow_id, "user_id": current_user['id']}, {"_id": 0})
    if not workflow:
        raise HTTPException(status_code=404, detail="Workflow not found")
    return WorkflowResponse(**workflow)


@api_router.put("/workflows/{workflow_id}/status", response_model=WorkflowResponse)
async def update_workflow_status(workflow_id: str, status: str, current_user: dict = Depends(get_current_user)):
    if status not in ['active', 'paused']:
        raise HTTPException(status_code=400, detail="Invalid status")
    
    result = await db.workflows.update_one(
        {"id": workflow_id, "user_id": current_user['id']},
        {"$set": {"status": status}}
    )
    
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Workflow not found")
    
    workflow = await db.workflows.find_one({"id": workflow_id}, {"_id": 0})
    return WorkflowResponse(**workflow)


@api_router.delete("/workflows/{workflow_id}", response_model=MessageResponse)
async def delete_workflow(workflow_id: str, current_user: dict = Depends(get_current_user)):
    result = await db.workflows.delete_one({"id": workflow_id, "user_id": current_user['id']})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Workflow not found")
    
    # Delete related enrollments
    await db.workflow_subscribers.delete_many({"workflow_id": workflow_id})
    
    return MessageResponse(message="Workflow deleted successfully")


@api_router.get("/workflows/{workflow_id}/subscribers")
async def get_workflow_subscribers(workflow_id: str, current_user: dict = Depends(get_current_user)):
    workflow = await db.workflows.find_one({"id": workflow_id, "user_id": current_user['id']}, {"_id": 0})
    if not workflow:
        raise HTTPException(status_code=404, detail="Workflow not found")
    
    enrollments = await db.workflow_subscribers.find({"workflow_id": workflow_id}, {"_id": 0}).to_list(1000)
    
    result = []
    for enrollment in enrollments:
        subscriber = await db.subscribers.find_one({"id": enrollment['subscriber_id']}, {"_id": 0})
        if subscriber:
            result.append({
                "subscriber": SubscriberResponse(**subscriber),
                "enrollment": enrollment
            })
    
    return result


# ==================== TRACKING ROUTES ====================

@api_router.get("/track/open/{campaign_id}/{subscriber_id}/{tracking_id}")
async def track_open(campaign_id: str, subscriber_id: str, tracking_id: str):
    """Track email opens via tracking pixel"""
    now = datetime.now(timezone.utc).isoformat()
    
    # Record open event
    open_doc = {
        "id": str(uuid.uuid4()),
        "campaign_id": campaign_id,
        "subscriber_id": subscriber_id,
        "tracking_id": tracking_id,
        "opened_at": now
    }
    await db.email_opens.insert_one(open_doc)
    
    # Check if this is a unique open
    existing_opens = await db.email_opens.count_documents({
        "campaign_id": campaign_id,
        "subscriber_id": subscriber_id
    })
    
    is_unique = existing_opens == 1
    
    # Update campaign stats
    update_ops = {"$inc": {"opens_count": 1}}
    if is_unique:
        update_ops["$inc"]["unique_opens"] = 1
    
    await db.campaigns.update_one({"id": campaign_id}, update_ops)
    
    # Update subscriber stats
    await db.subscribers.update_one(
        {"id": subscriber_id},
        {"$inc": {"opens_count": 1}, "$set": {"last_activity": now}}
    )
    
    # Trigger email_opened workflows
    subscriber = await db.subscribers.find_one({"id": subscriber_id}, {"_id": 0})
    if subscriber:
        await trigger_workflow_event(subscriber['user_id'], "email_opened", subscriber_id, {"campaign_id": campaign_id})
    
    # Return 1x1 transparent GIF
    gif_bytes = base64.b64decode('R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7')
    return Response(content=gif_bytes, media_type="image/gif")


@api_router.get("/track/click/{campaign_id}/{subscriber_id}")
async def track_click(campaign_id: str, subscriber_id: str, url: str):
    """Track link clicks and redirect"""
    now = datetime.now(timezone.utc).isoformat()
    
    # Record click event
    click_doc = {
        "id": str(uuid.uuid4()),
        "campaign_id": campaign_id,
        "subscriber_id": subscriber_id,
        "url": url,
        "clicked_at": now
    }
    await db.link_clicks.insert_one(click_doc)
    
    # Check if this is a unique click
    existing_clicks = await db.link_clicks.count_documents({
        "campaign_id": campaign_id,
        "subscriber_id": subscriber_id
    })
    
    is_unique = existing_clicks == 1
    
    # Update campaign stats
    update_ops = {"$inc": {"clicks_count": 1}}
    if is_unique:
        update_ops["$inc"]["unique_clicks"] = 1
    
    await db.campaigns.update_one({"id": campaign_id}, update_ops)
    
    # Update subscriber stats
    await db.subscribers.update_one(
        {"id": subscriber_id},
        {"$inc": {"clicks_count": 1}, "$set": {"last_activity": now}}
    )
    
    # Trigger link_clicked workflows
    subscriber = await db.subscribers.find_one({"id": subscriber_id}, {"_id": 0})
    if subscriber:
        await trigger_workflow_event(subscriber['user_id'], "link_clicked", subscriber_id, {"campaign_id": campaign_id, "url": url})
    
    return RedirectResponse(url=url, status_code=302)


# ==================== DASHBOARD ROUTES ====================

@api_router.get("/dashboard/stats", response_model=DashboardStats)
async def get_dashboard_stats(current_user: dict = Depends(get_current_user)):
    user_id = current_user['id']
    
    stats = await asyncio.gather(
        db.subscribers.count_documents({"user_id": user_id}),
        db.campaigns.count_documents({"user_id": user_id}),
        db.campaigns.count_documents({"user_id": user_id, "status": "sent"}),
        db.campaigns.count_documents({"user_id": user_id, "status": "draft"}),
        db.tags.count_documents({"user_id": user_id}),
        db.segments.count_documents({"user_id": user_id}),
        db.workflows.count_documents({"user_id": user_id}),
        db.workflows.count_documents({"user_id": user_id, "status": "active"})
    )
    
    # Get total opens and clicks from campaigns
    pipeline = [
        {"$match": {"user_id": user_id}},
        {"$group": {"_id": None, "total_opens": {"$sum": "$opens_count"}, "total_clicks": {"$sum": "$clicks_count"}}}
    ]
    agg_result = await db.campaigns.aggregate(pipeline).to_list(1)
    total_opens = agg_result[0]['total_opens'] if agg_result else 0
    total_clicks = agg_result[0]['total_clicks'] if agg_result else 0
    
    return DashboardStats(
        total_subscribers=stats[0],
        total_campaigns=stats[1],
        sent_campaigns=stats[2],
        draft_campaigns=stats[3],
        total_tags=stats[4],
        total_segments=stats[5],
        total_workflows=stats[6],
        active_workflows=stats[7],
        total_opens=total_opens,
        total_clicks=total_clicks
    )


# ==================== HEALTH CHECK ====================

@api_router.get("/")
async def root():
    return {"message": "EmailBlast API is running"}


@api_router.get("/health")
async def health():
    return {"status": "healthy", "service": "emailblast-api"}


# Include the router in the main app
app.include_router(api_router)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get('CORS_ORIGINS', '*').split(','),
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("shutdown")
async def shutdown_db_client():
    client.close()
