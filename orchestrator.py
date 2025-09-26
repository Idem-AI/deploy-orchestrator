#!/usr/bin/env python3
"""
orchestrator_minimal.py avec streaming des logs en temps réel
"""
import uuid
import os
import time
import subprocess
import tempfile
from typing import Optional, Dict, Any
from fastapi import FastAPI, HTTPException, Header, File, UploadFile, Form, Body
from fastapi.responses import StreamingResponse
import json
from pydantic import BaseModel, Field
from fastapi.responses import JSONResponse
from urllib.parse import urlparse
# --- utils file storage (atomic read/write) ---
import threading

_fs_lock = threading.Lock()

def read_json(path: str):
    """
    Lit un fichier JSON et retourne le dict.
    Si fichier manquant, renvoie {}.
    Protégé par un lock pour éviter corruptions.
    """
    with _fs_lock:
        try:
            if not os.path.exists(path):
                return {}
            with open(path, "r") as fh:
                return json.load(fh)
        except Exception as e:
            # propagate a clear error to server logs
            raise RuntimeError(f"failed to read json {path}: {e}")

def write_json(path: str, data):
    """
    Écrit atomiquement un objet JSON dans `path`.
    """
    dirn = os.path.dirname(path) or "."
    os.makedirs(dirn, exist_ok=True)
    tmp = path + ".tmp"
    with _fs_lock:
        with open(tmp, "w") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except Exception:
            pass

app = FastAPI(title="Orchestrator Deployment", version="1.2")

# Config via env
DEPLOY_CERT_SCRIPT = os.environ.get("DEPLOY_CERT_SCRIPT", "/opt/vps-deployment/deploy_app_with_certs.sh")
DEPLOY_SPA_SCRIPT  = os.environ.get("DEPLOY_SPA_SCRIPT",  "/opt/vps-deployment/deploy-spa-app.sh")
ADMIN_API_TOKEN    = os.environ.get("ADMIN_API_TOKEN")  # if set, API requires header X-ADMIN-TOKEN
DEFAULT_TIMEOUT    = int(os.environ.get("DEFAULT_TIMEOUT", "1800"))  # seconds
APPS_BASE          = os.environ.get("APPS_BASE", "/opt/vps-deployment/apps")

class DeployReq(BaseModel):
    repo_url: str
    domain: Optional[str] = None
    is_spa: Optional[bool] = False
    timeout_seconds: Optional[int] = None
    env: Optional[Dict[str, Any]] = Field(None, description="Optional mapping of environment variables to write into .env")

def require_admin(token_header: Optional[str]):
    if ADMIN_API_TOKEN:
        if not token_header or token_header != ADMIN_API_TOKEN:
            raise HTTPException(status_code=403, detail="invalid admin token")

def sanitize_app_name(repo_url: str) -> str:
    try:
        parsed = urlparse(repo_url)
        path = parsed.path or repo_url
    except Exception:
        path = repo_url
    base = os.path.basename(path)
    if base.endswith(".git"):
        base = base[:-4]
    if not base or "/" in base or "\\" in base or ".." in base:
        raise HTTPException(status_code=400, detail=f"cannot derive safe app name from repo_url: {repo_url}")
    return base

def dict_to_env_bytes(env: Dict[str, Any]) -> bytes:
    lines = []
    for k, v in env.items():
        if not isinstance(k, str) or k.strip() == "":
            continue
        s = "" if v is None else str(v)
        safe = s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        lines.append(f'{k}="{safe}"')
    return ("\n".join(lines) + "\n").encode("utf-8")

def atomic_write(path: str, data: bytes, mode: int = 0o600):
    dirn = os.path.dirname(path)
    os.makedirs(dirn, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dirn)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
        os.chmod(path, mode)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except Exception:
                pass

def stream_script(script_path: str, args: list, timeout: int):
    """Exécute un script et yield ses logs en direct"""
    if not os.path.isfile(script_path) or not os.access(script_path, os.X_OK):
        yield f"ERROR: script not found or not executable: {script_path}\n"
        return

    cmd = [script_path] + args
    try:
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for line in process.stdout:
            yield line  # stream vers client HTTP
        process.wait(timeout=timeout)
        yield f"\n--- Process exited with code {process.returncode} ---\n"
    except subprocess.TimeoutExpired:
        process.kill()
        yield f"\n--- ERROR: script timeout after {timeout} seconds ---\n"

@app.get("/")
def root():
    return {"ok": True, "note": "Orchestrator - streaming logs enabled."}

@app.post("/deploy")
async def deploy(
    repo_url: str = Form(...),
    domain: str = Form(None),
    is_spa: bool = Form(False),
    env_file: UploadFile = File(None),
    x_admin_token: str = Header(None)
):
    require_admin(x_admin_token)

    env_dict = {}
    if env_file:
        contents = await env_file.read()
        env_dict = json.loads(contents.decode("utf-8"))

    if env_dict:
        env_bytes = dict_to_env_bytes(env_dict)
        env_path = os.path.join(APPS_BASE, ".env")
        atomic_write(env_path, env_bytes, mode=0o600)

    script = DEPLOY_SPA_SCRIPT if is_spa else DEPLOY_CERT_SCRIPT
    args = [repo_url]
    if domain:
        args.append(domain)

    return StreamingResponse(stream_script(script, args, DEFAULT_TIMEOUT), media_type="text/plain")


# ---- Cas B endpoints (agents) ----
# --- storage paths and init for Case B (agents/jobs/envs) ---
ORCH_B_STORAGE = os.environ.get("ORCH_B_STORAGE", "/var/lib/orch_b")
AGENTS_FILE = os.path.join(ORCH_B_STORAGE, "agents.json")
JOBS_FILE   = os.path.join(ORCH_B_STORAGE, "jobs.json")
ENVS_DIR    = os.path.join(ORCH_B_STORAGE, "envs")

# Ensure storage directories/files exist and are writable by the process user
os.makedirs(ENVS_DIR, exist_ok=True)

# create empty JSON files if missing
if not os.path.exists(AGENTS_FILE):
    try:
        with open(AGENTS_FILE, "w") as fh:
            json.dump({}, fh)
        os.chmod(AGENTS_FILE, 0o600)
    except Exception:
        # If permission denied here, the process user likely can't write to ORCH_B_STORAGE.
        raise RuntimeError(f"cannot create {AGENTS_FILE}; check ORCH_B_STORAGE permissions")

if not os.path.exists(JOBS_FILE):
    try:
        with open(JOBS_FILE, "w") as fh:
            json.dump({}, fh)
        os.chmod(JOBS_FILE, 0o600)
    except Exception:
        raise RuntimeError(f"cannot create {JOBS_FILE}; check ORCH_B_STORAGE permissions")


class RegisterReq(BaseModel):
    hostname: str
    ip: Optional[str] = ""
    ssh_pubkey: Optional[str] = ""
    meta: Optional[Dict[str, Any]] = {}

@app.post("/register")
def register(req: RegisterReq):
    agents = read_json(AGENTS_FILE)
    # create agent_id and token
    agent_id = str(uuid.uuid4())
    agent_token = uuid.uuid4().hex
    agents[agent_id] = {
        "agent_id": agent_id,
        "token": agent_token,
        "hostname": req.hostname,
        "ip": req.ip or "",
        "ssh_pubkey": req.ssh_pubkey or "",
        "meta": req.meta or {},
        "created": int(time.time()),
        "last_seen": int(time.time())
    }
    write_json(AGENTS_FILE, agents)
    return {"agent_id": agent_id, "agent_token": agent_token}

@app.get("/agents")
def list_agents(x_admin_token: Optional[str] = Header(None)):
    require_admin(x_admin_token)
    agents = read_json(AGENTS_FILE)
    return {"agents": agents}

@app.post("/create_job")
def create_job(payload: Dict[str, Any], x_admin_token: Optional[str] = Header(None)):
    """
    payload expected:
      {
        "agent_id": "...",
        "is_spa": "True or False",
        "args": ["https://repo.git","domain.tld"],
        "env_token": "optional token referencing uploaded env"
      }
    """
    require_admin(x_admin_token)
    agents = read_json(AGENTS_FILE)
    jobs = read_json(JOBS_FILE)
    agent_id = payload.get("agent_id")
    if agent_id not in agents:
        raise HTTPException(status_code=404, detail="agent not found")
    args = payload.get("args", [])
    env_token = payload.get("env_token")
    is_spa = payload.get("is_spa", False)
    job_id = str(uuid.uuid4())
    jobs[job_id] = {
        "job_id": job_id,
        "agent_id": agent_id,
        "args": args,
        "is_spa": is_spa,
        "env_token": env_token,
        "status": "pending",
        "created": int(time.time()),
        "updated": int(time.time())
    }
    write_json(JOBS_FILE, jobs)
    return {"job_id": job_id}

@app.get("/jobs")
def list_jobs(x_admin_token: Optional[str] = Header(None)):
    require_admin(x_admin_token)
    jobs = read_json(JOBS_FILE)
    return {"jobs": jobs}

@app.get("/poll_jobs")
def poll_jobs(x_agent_token: Optional[str] = Header(None)):
    if not x_agent_token:
        raise HTTPException(status_code=403, detail="missing agent token")
    agents = read_json(AGENTS_FILE)
    agent_id = None
    for aid, a in agents.items():
        if a.get("token") == x_agent_token:
            agent_id = aid
            break
    if not agent_id:
        raise HTTPException(status_code=403, detail="invalid agent token")
    # update last_seen
    agents[agent_id]["last_seen"] = int(time.time())
    write_json(AGENTS_FILE, agents)
    jobs = read_json(JOBS_FILE)
    pending = []
    for jid, job in jobs.items():
        if job.get("agent_id") == agent_id and job.get("status") == "pending":
            pending.append(job)
    return {"jobs": pending}

class ReportReq(BaseModel):
    job_id: str
    status: str  # "done"|"failed"
    output: Optional[str] = ""

@app.post("/report")
def report(req: ReportReq, x_agent_token: Optional[str] = Header(None)):
    if not x_agent_token:
        raise HTTPException(status_code=403, detail="missing agent token")
    agents = read_json(AGENTS_FILE)
    agent_id = None
    for aid,a in agents.items():
        if a.get("token") == x_agent_token:
            agent_id = aid
            break
    if not agent_id:
        raise HTTPException(status_code=403, detail="invalid agent token")
    jobs = read_json(JOBS_FILE)
    if req.job_id not in jobs:
        raise HTTPException(status_code=404, detail="job not found")
    job = jobs[req.job_id]
    if job.get("agent_id") != agent_id:
        raise HTTPException(status_code=403, detail="job does not belong to this agent")
    job["status"] = req.status
    job["output"] = req.output
    job["updated"] = int(time.time())
    write_json(JOBS_FILE, jobs)
    return {"ok": True}

# ---- env upload / download for big env files ----

@app.post("/upload_env")
async def upload_env_file(env_file: UploadFile = File(None), env_json: Optional[Dict[str, Any]] = Body(None), x_admin_token: Optional[str] = Header(None)):
    """
    Admin uploads a large env.json (multipart file or raw JSON body).
    Returns { env_token: "..." } to reference in create_job.
    """
    require_admin(x_admin_token)
    if env_file:
        contents = await env_file.read()
        try:
            data = json.loads(contents.decode("utf-8"))
        except Exception:
            raise HTTPException(status_code=400, detail="uploaded file not valid JSON")
    elif env_json is not None:
        data = env_json
    else:
        raise HTTPException(status_code=400, detail="no env provided")

    env_token = "env_" + uuid.uuid4().hex
    out_path = os.path.join(ORCH_B_STORAGE, "envs", f"{env_token}.json")
    # save atomically
    tmp = out_path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh)
    os.replace(tmp, out_path)
    os.chmod(out_path, 0o600)
    return {"env_token": env_token}

@app.get("/download_env")
def download_env(env_token: str, x_agent_token: Optional[str] = Header(None)):
    """
    Agent downloads env JSON referenced by env_token.
    Validation: ensure there's a pending job for this agent that references this env_token
    """
    if not x_agent_token:
        raise HTTPException(status_code=403, detail="missing agent token")
    agents = read_json(AGENTS_FILE)
    agent_id = None
    for aid,a in agents.items():
        if a.get("token") == x_agent_token:
            agent_id = aid
            break
    if not agent_id:
        raise HTTPException(status_code=403, detail="invalid agent token")

    # verify that at least one pending job for this agent references env_token
    jobs = read_json(JOBS_FILE)
    ok = False
    for job in jobs.values():
        if job.get("agent_id") == agent_id and job.get("status") == "pending" and job.get("env_token") == env_token:
            ok = True
            break
    if not ok:
        raise HTTPException(status_code=403, detail="no job for this agent references this env token")

    path = os.path.join(ORCH_B_STORAGE, "envs", f"{env_token}.json")
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="env token not found")
    with open(path, "r") as fh:
        data = json.load(fh)
    return JSONResponse(content=data)

# ---- end ----
