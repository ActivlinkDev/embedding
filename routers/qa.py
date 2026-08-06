# routers/qa.py — transcription + QA scoring (Whisper + GPT)
# - Standalone (no customer linking)
# - Uses script_name only (no script_config_json)
# - Deterministic, server-side proportional scoring:
#     section_score = 100 * (# required met) / (# required total)
#   If a section has no required checkpoints:
#     score = 100 * (# optional met) / (# optional total), else 100 if no checks at all.

import os, io, json, anyio, sys, pathlib, logging
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any

from fastapi import APIRouter, UploadFile, File, Form, Depends, HTTPException, Query
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from bson import ObjectId
from openai import OpenAI

# ---------- Logging ----------
logger = logging.getLogger("qa")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)

# ---------- Ensure project root on sys.path ----------
ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ---------- Auth: utils.dependencies → dependencies → fallback ----------
from utils.api_docs import error, json_response, secured

verify_token = None
try:
    from utils.dependencies import verify_token as _vt  # preferred (your utils folder)
    verify_token = _vt
    logger.info("[QA] Using auth from utils.dependencies.verify_token")
except Exception:
    try:
        from dependencies import verify_token as _vt  # root fallback
        verify_token = _vt
        logger.info("[QA] Using auth from dependencies.verify_token (root)")
    except Exception:
        from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
        API_TOKEN = os.getenv("API_TOKEN")
        if not API_TOKEN:
            raise RuntimeError("API_TOKEN not set in environment/.env and no verify_token available")

        security = HTTPBearer()
        def verify_token(credentials: HTTPAuthorizationCredentials = Depends(security)):
            token = credentials.credentials
            if token != API_TOKEN:
                raise HTTPException(status_code=401, detail="Invalid or missing token")
        logger.warning("[QA] verify_token not found; using internal lightweight bearer")

router = APIRouter(prefix="/qa", tags=["QA"], dependencies=[Depends(verify_token)])

# ---------- DB / OpenAI ----------
_mongo_client = AsyncIOMotorClient(os.getenv("MONGO_URI"))
_db: AsyncIOMotorDatabase = _mongo_client[os.getenv("MONGO_DB", "Activlink")]

COL_TRANSCRIPTS = "qa_transcripts"
COL_RESULTS = "qa_results"
COL_SCRIPTS = "qa_scripts"

_oai = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
_WHISPER_MODEL = os.getenv("WHISPER_MODEL", "whisper-1")
_SCORING_MODEL = os.getenv("OPENAI_SCORING_MODEL", "gpt-4o-mini")

# ---------- Utils ----------
def _as_obj_id(s: str) -> ObjectId:
    try:
        return ObjectId(s)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid ObjectId")

# ---------- OpenAI Calls ----------
async def _transcribe_openai(file_like: io.BytesIO, language: Optional[str]) -> Dict[str, Any]:
    def _call():
        return _oai.audio.transcriptions.create(
            model=_WHISPER_MODEL,
            file=file_like,
            language=language,
            response_format="json",
        )
    resp = await anyio.to_thread.run_sync(_call)
    return {
        "text": resp.text,
        "raw": resp.model_dump() if hasattr(resp, "model_dump") else dict(resp),
        "language": language or "auto",
    }

async def _score_openai(transcript_text: str, script_config: dict, model: Optional[str]) -> dict:
    """
    Ask the model ONLY for boolean checkpoint results + evidence.
    We DO NOT trust numeric scores from the model; we compute them server-side.
    """
    system_prompt = (
        "You are a meticulous QA analyst for contact-centre calls. "
        "Return STRICT JSON ONLY with keys: sections, prohibited_flags, key_misses, final. "
        "For each section, return checks[] with {id, met:boolean, evidence?, notes?}. "
        "DO NOT assign numeric scores; the server will calculate them."
    )

    user_msg = f"""
SCRIPT CONFIG (JSON):
{json.dumps(script_config, ensure_ascii=False)}

TRANSCRIPT (verbatim):
\"\"\"{transcript_text}\"\"\"

Instructions:
1) For each section in script_config.checkpoints, return checks[] with met=true/false and short evidence (<=20 words) when available.
2) List required checkpoints that are not met in key_misses (unique IDs).
3) If anything in 'prohibited' appears or is implied, add to prohibited_flags (quote a short phrase if possible).
4) Do NOT return numeric scores; leave any numeric fields at 0 or omit them. The server will calculate scores.
5) Return JSON ONLY: {{sections, prohibited_flags, key_misses, final}}.
"""

    def _call():
        comp = _oai.chat.completions.create(
            model=model or _SCORING_MODEL,
            temperature=0,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_msg},
            ],
            response_format={"type": "json_object"},
        )
        return comp.choices[0].message.content

    content = await anyio.to_thread.run_sync(_call)

    try:
        return json.loads(content)
    except json.JSONDecodeError:
        cleaned = content[content.find("{"): content.rfind("}") + 1]
        return json.loads(cleaned)

# ---------- Deterministic Proportional Scoring ----------
def _calc_section_score_proportional(checks: list, script_section_cfg: list) -> float:
    """
    Proportional scoring by REQUIRED checkpoints only.
    - Score = 100 * (# required met) / (# required total)
    - If no required checkpoints exist:
        - If there are optionals, score = 100 * (# optional met) / (# optional total)
        - Else (no checks at all), score = 100
    """
    required_ids = {c["id"] for c in script_section_cfg if c.get("required", False)}
    optional_ids = {c["id"] for c in script_section_cfg if not c.get("required", False)}
    met_ids = {c.get("id") for c in checks if c.get("met") is True}

    req_total = len(required_ids)
    if req_total > 0:
        req_met = len(required_ids & met_ids)
        return round(100.0 * req_met / req_total, 2)

    opt_total = len(optional_ids)
    if opt_total > 0:
        opt_met = len(optional_ids & met_ids)
        return round(100.0 * opt_met / opt_total, 2)

    return 100.0

def _recalculate_scores(model_result: dict, script_config: dict) -> dict:
    """
    Recalculate per-section scores and final weighted score using proportional REQUIRED logic.
    Optional: apply a global penalty for prohibited flags (currently disabled).
    """
    sections_out = {}
    weights = script_config.get("weights", {})
    checkpoints_cfg = script_config.get("checkpoints", {})
    total_weight = sum(weights.values()) or 1.0

    # 1) Section scores (proportional by required checks)
    for sec_name, sec_checks_cfg in checkpoints_cfg.items():
        sec_result = (model_result.get("sections") or {}).get(sec_name) or {}
        checks = sec_result.get("checks") or []
        sec_score = _calc_section_score_proportional(checks, sec_checks_cfg)
        sections_out[sec_name] = {
            "score": sec_score,
            "passed": sec_score >= 70.0,  # pass threshold (tune if you like)
            "checks": checks,
        }

    # 2) Weighted final score
    weighted_sum = 0.0
    for sec_name, sec_data in sections_out.items():
        w = float(weights.get(sec_name, 0.0))
        weighted_sum += w * sec_data["score"]
    final_weighted = round((weighted_sum / total_weight), 2)

    # 3) Optional global penalty for prohibited flags (disabled by default)
    prohibited = model_result.get("prohibited_flags") or []
    # Example penalty (commented out):
    # if prohibited:
    #     final_weighted = min(final_weighted, 40.0)

    return {
        "sections": sections_out,
        "prohibited_flags": prohibited,
        "key_misses": model_result.get("key_misses") or [],
        "final": {
            "weighted_score": final_weighted,
            "summary": (model_result.get("final") or {}).get("summary", "")
        }
    }

# ---------- Routes ----------

@router.post(
    "/transcribe",
    summary="Transcribe a call recording",
    response_description="The stored transcript, its id, and the language detected.",
    responses=secured({
        200: json_response(
            "The recording was transcribed and stored.",
            {
                "transcript_id": "6900a1b2c3d4e5f6a7b80011",
                "language": "en",
                "text": "Good morning, you're through to Acme. My name is Sam…",
            },
        ),
        500: error("Transcription failed. Nothing was stored — retry.", "Transcription failed: ..."),
    }),
)
async def transcribe(
    file: UploadFile = File(..., description="**Mandatory.** The audio file to transcribe — mp3, wav or m4a. Sent as multipart form data."),
    language: Optional[str] = Form(
        None,
        description="Language hint, e.g. `en`. Detected automatically when omitted; set it for short or noisy audio where detection is unreliable.",
    ),
):
    """
    Transcribe a call recording to text and store it.

    Send the audio as **multipart form data**, not JSON. `file` is mandatory; `language` is an
    optional hint that improves accuracy on short or noisy recordings.

    The transcript is stored and its `transcript_id` returned — pass that to `POST /qa/score` to
    have the call assessed. To do both in one request, use `POST /qa/process` instead.

    Transcription is model-generated and imperfect; treat the text as evidence to review, not as
    a verbatim record.
    """
    data = await file.read()
    fobj = io.BytesIO(data); fobj.name = file.filename or "audio.wav"

    try:
        tresp = await _transcribe_openai(fobj, language)
    except Exception as e:
        logger.error(f"[QA] Transcription failed: {e}")
        raise HTTPException(status_code=500, detail=f"Transcription failed: {e}")

    doc = {
        "created_at": datetime.now(timezone.utc),
        "filename": file.filename,
        "language": tresp["language"],
        "text": tresp["text"],
        "provider_raw": tresp["raw"],
        "whisper_model": _WHISPER_MODEL,
    }
    res = await _db[COL_TRANSCRIPTS].insert_one(doc)

    return {
        "transcript_id": str(res.inserted_id),
        "language": doc["language"],
        "text": doc["text"],
    }

@router.post(
    "/score",
    summary="Score a transcript against a named script",
    response_description="The stored result id and the full scored breakdown.",
    responses=secured({
        200: json_response(
            "The transcript was scored and the result stored.",
            {
                "result_id": "6900b2c3d4e5f6a7b8c90022",
                "transcript_id": "6900a1b2c3d4e5f6a7b80011",
                "payload": {
                    "sections": {
                        "opening": {
                            "score": 100.0,
                            "passed": True,
                            "checks": [
                                {"id": "greeting", "met": True, "evidence": "Good morning, you're through to Acme."},
                                {"id": "identify_agent", "met": True, "evidence": "My name is Sam."},
                            ],
                        },
                        "compliance": {
                            "score": 50.0,
                            "passed": False,
                            "checks": [
                                {"id": "state_recording", "met": True, "evidence": "This call is recorded."},
                                {"id": "confirm_consent", "met": False, "evidence": ""},
                            ],
                        },
                    },
                    "prohibited_flags": [],
                    "key_misses": ["confirm_consent"],
                    "final": {"weighted_score": 72.5, "summary": "Strong opening; consent was not confirmed."},
                },
            },
        ),
        400: error("Neither `transcript_text` nor `transcript_id` was supplied.", "Provide transcript_text or transcript_id"),
        404: error("No such transcript, or no script config with that name.", "script_name not found"),
        500: error("Scoring failed. Nothing was stored — retry.", "Scoring failed: ..."),
    }),
)
async def score(
    transcript_text: Optional[str] = Query(
        None,
        description="The transcript to score, inline. **Either this or `transcript_id` is required**; this one wins if both are sent.",
    ),
    transcript_id: Optional[str] = Query(
        None,
        description="Id of a transcript stored by `POST /qa/transcribe`. Used when `transcript_text` is absent.",
    ),
    script_name: str = Form(..., description="**Mandatory.** Name of a script config saved via `POST /qa/scripts`. Unknown names return `404`."),
    model: Optional[str] = Query(None, description="Override the scoring model. Defaults to the configured one."),
):
    """
    Score a call transcript against a named script config — the checklist of things an agent was
    supposed to say.

    Supply the transcript **either** inline as `transcript_text` **or** by `transcript_id`; one of
    the two is required, and the inline text wins if both are given. `script_name` is mandatory
    and must name a config saved via `POST /qa/scripts`.

    **Scoring is deliberately split.** The model only decides whether each checkpoint was met and
    quotes the evidence; the numbers are then recalculated server-side from the script's weights,
    so the same transcript and script always produce the same score.

    In the result, each section scores the proportion of its **required** checks that were met and
    passes at 70; `final.weighted_score` combines sections using the script's weights.
    `key_misses` lists the required checks that failed, and `prohibited_flags` records anything
    forbidden that was said — currently reported without affecting the score.

    Every call stores a result and returns its `result_id`; retrieve it later with
    `GET /qa/result/{result_id}`.
    """
    # Resolve transcript
    if not transcript_text and transcript_id:
        tdoc = await _db[COL_TRANSCRIPTS].find_one({"_id": _as_obj_id(transcript_id)})
        if not tdoc:
            raise HTTPException(status_code=404, detail="transcript_id not found")
        transcript_text = tdoc["text"]

    if not transcript_text:
        raise HTTPException(status_code=400, detail="Provide transcript_text or transcript_id")

    # Resolve script config by name
    sdoc = await _db[COL_SCRIPTS].find_one({"name": script_name})
    if not sdoc:
        raise HTTPException(status_code=404, detail="script_name not found")
    script_config = sdoc["config"]

    # Model → booleans + evidence
    try:
        model_raw = await _score_openai(transcript_text, script_config, model)
    except Exception as e:
        logger.error(f"[QA] Scoring failed: {e}")
        raise HTTPException(status_code=500, detail=f"Scoring failed: {e}")

    # Deterministic server-side scoring
    result = _recalculate_scores(model_raw, script_config)

    # Persist
    doc = {
        "created_at": datetime.now(timezone.utc),
        "transcript_id": transcript_id,
        "script_config": script_config,
        "model": model or _SCORING_MODEL,
        "payload": result,
    }
    res = await _db[COL_RESULTS].insert_one(doc)

    return {
        "result_id": str(res.inserted_id),
        "transcript_id": transcript_id,
        "payload": result,
    }

@router.post(
    "/process",
    summary="Transcribe and score a recording in one call",
    response_description="The same payload as `POST /qa/score`: the result id and the scored breakdown.",
    responses=secured({
        200: json_response(
            "The recording was transcribed and scored.",
            {
                "result_id": "6900b2c3d4e5f6a7b8c90022",
                "transcript_id": "6900a1b2c3d4e5f6a7b80011",
                "payload": {
                    "sections": {
                        "opening": {
                            "score": 100.0,
                            "passed": True,
                            "checks": [
                                {"id": "greeting", "met": True, "evidence": "Good morning, you're through to Acme."},
                                {"id": "identify_agent", "met": True, "evidence": "My name is Sam."},
                            ],
                        },
                        "compliance": {
                            "score": 50.0,
                            "passed": False,
                            "checks": [
                                {"id": "state_recording", "met": True, "evidence": "This call is recorded."},
                                {"id": "confirm_consent", "met": False, "evidence": ""},
                            ],
                        },
                    },
                    "prohibited_flags": [],
                    "key_misses": ["confirm_consent"],
                    "final": {"weighted_score": 72.5, "summary": "Strong opening; consent was not confirmed."},
                },
            },
        ),
        404: error("No script config with that name.", "script_name not found"),
        500: error("Transcription or scoring failed.", "Transcription failed: ..."),
    }),
)
async def process_audio(
    file: UploadFile = File(..., description="**Mandatory.** The audio file to transcribe and score — mp3, wav or m4a."),
    language: Optional[str] = Form(None, description="Language hint for transcription. Detected automatically when omitted."),
    script_name: str = Form(..., description="**Mandatory.** Name of the script config to score against."),
    model: Optional[str] = Query(None, description="Override the scoring model. Defaults to the configured one."),
):
    """
    Upload a recording and get it transcribed **and** scored in one request — the usual entry
    point for QA.

    Equivalent to calling `POST /qa/transcribe` then `POST /qa/score` with the resulting
    transcript id, and it returns exactly what `/qa/score` returns. Sent as **multipart form
    data**.

    `file` and `script_name` are mandatory; the script must already exist (`POST /qa/scripts`).

    Both steps store their output, so a failure during scoring still leaves the transcript
    behind — the wasted work is the transcription, not the audio. Scoring itself is deterministic
    server-side; see `POST /qa/score` for how the numbers are produced.
    """
    t = await transcribe(file=file, language=language)
    return await score(
        transcript_text=None,
        transcript_id=t["transcript_id"],
        script_name=script_name,
        model=model,
    )

@router.get(
    "/result/{result_id}",
    summary="Retrieve a stored QA result",
    response_description="The scored breakdown as stored, with the model used and when it ran.",
    responses=secured({
        200: json_response(
            "The result was found.",
            {
                "result_id": "6900b2c3d4e5f6a7b8c90022",
                "transcript_id": "6900a1b2c3d4e5f6a7b80011",
                "payload": {
                    "sections": {
                        "opening": {
                            "score": 100.0,
                            "passed": True,
                            "checks": [
                                {"id": "greeting", "met": True, "evidence": "Good morning, you're through to Acme."},
                                {"id": "identify_agent", "met": True, "evidence": "My name is Sam."},
                            ],
                        },
                        "compliance": {
                            "score": 50.0,
                            "passed": False,
                            "checks": [
                                {"id": "state_recording", "met": True, "evidence": "This call is recorded."},
                                {"id": "confirm_consent", "met": False, "evidence": ""},
                            ],
                        },
                    },
                    "prohibited_flags": [],
                    "key_misses": ["confirm_consent"],
                    "final": {"weighted_score": 72.5, "summary": "Strong opening; consent was not confirmed."},
                },
                "created_at": "2026-08-06T10:14:52.113000+00:00",
                "model": "gpt-4o",
            },
        ),
        404: error("No result with this id.", "result_id not found"),
    }),
)
async def get_result(result_id: str):
    """
    Fetch a QA result produced earlier by `POST /qa/score` or `POST /qa/process`.

    Path parameter `result_id` is mandatory. The `payload` is returned **as it was scored** —
    nothing is recalculated on read, so a later change to the script config does not alter
    historic results.

    `model` records which model produced the judgements, which matters when comparing scores
    across time.
    """
    doc = await _db[COL_RESULTS].find_one({"_id": _as_obj_id(result_id)})
    if not doc:
        raise HTTPException(status_code=404, detail="result_id not found")
    return {
        "result_id": result_id,
        "transcript_id": doc.get("transcript_id"),
        "payload": doc.get("payload"),
        "created_at": doc.get("created_at"),
        "model": doc.get("model"),
    }

@router.post(
    "/scripts",
    summary="Create or replace a named script config",
    response_description="The name the config was stored under.",
    responses=secured({
        200: json_response("The config was stored.", {"name": "retention-call-v3"}),
        400: error("`config_json` is not valid JSON.", "Invalid config_json: Expecting value: line 1 column 1"),
    }),
)
async def save_script(
    name: str = Form(..., description="**Mandatory.** Config name. Saving under an existing name **replaces** it."),
    config_json: str = Form(..., description="**Mandatory.** The config as a JSON **string**, holding `checkpoints`, `weights` and optional `metadata`."),
):
    """
    Create or replace the script config a call is scored against — the checkpoints an agent is
    expected to hit and how much each section counts.

    Sent as **multipart form data**, with the config as a **JSON string** in `config_json`, not
    as a nested object. It must contain `checkpoints` (sections, each listing its checks and
    which are required) and `weights` (a weight per section); `metadata` is optional and is what
    `GET /qa/scripts` lists.

    **Saving under an existing name overwrites it** — this is an upsert with no versioning and
    no confirmation. Results already scored keep the config they used, so history is unaffected,
    but scores taken before and after a change are not comparable.

    Only the JSON syntax is validated; a config missing `checkpoints` or `weights` is stored
    happily and fails later at scoring time.
    """
    try:
        cfg = json.loads(config_json)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid config_json: {e}")

    await _db[COL_SCRIPTS].update_one(
        {"name": name},
        {"$set": {"name": name, "config": cfg, "updated_at": datetime.now(timezone.utc)}},
        upsert=True,
    )
    return {"name": name}

@router.get(
    "/scripts",
    summary="List the available script configs",
    response_description="Every stored config: its name and metadata only.",
    responses=secured({
        200: json_response(
            "The stored configs, sorted by name. The checkpoints themselves are not included.",
            [
                {"name": "retention-call-v3", "metadata": {"owner": "QA team", "updated": "2026-07-14"}},
                {"name": "sales-outbound-v1", "metadata": {"owner": "Sales", "updated": "2026-03-02"}},
            ],
        ),
    }),
)
async def list_scripts():
    """
    List the script configs available to score against, sorted by name.

    Takes no parameters. Only `name` and `metadata` are returned — **not** the checkpoints or
    weights, so this is a picker, not a way to read a config back. Use the names here as
    `script_name` on `POST /qa/score` and `POST /qa/process`.
    """
    cur = _db[COL_SCRIPTS].find({}, {"name": 1, "config.metadata": 1}).sort("name", 1)
    out: List[Dict[str, Any]] = []
    async for d in cur:
        out.append({"name": d["name"], "metadata": (d.get("config", {}).get("metadata", {}))})
    return out
