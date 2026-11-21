"""ClipPro server: Edge-TTS (free voices) + FFmpeg (cut / merge / dub)."""
import asyncio, json, os, re, shutil, subprocess, tempfile, uuid
import edge_tts
from fastapi import FastAPI, File, Form, HTTPException, UploadFile, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

app = FastAPI(title="ClipPro Server")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

API_TOKEN = os.environ.get("CLIPPRO_TOKEN", "")  # optional: set to protect your server
MAX_MB = int(os.environ.get("MAX_UPLOAD_MB", "300"))

def check(token):
    if API_TOKEN and token != API_TOKEN:
        raise HTTPException(401, "bad token")

def run(cmd):
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise HTTPException(500, "ffmpeg: " + p.stderr[-400:])

async def save_upload(up: UploadFile, folder: str, name: str) -> str:
    path = os.path.join(folder, name); size = 0
    with open(path, "wb") as f:
        while chunk := await up.read(1 << 20):
            size += len(chunk)
            if size > MAX_MB << 20:
                raise HTTPException(413, f"file > {MAX_MB}MB")
            f.write(chunk)
    return path

def cleanup(folder): shutil.rmtree(folder, ignore_errors=True)

@app.get("/")
def health():
    return {"ok": True, "ffmpeg": shutil.which("ffmpeg") is not None}

class TTSReq(BaseModel):
    text: str
    voice: str = ""
    gender: str = "f"          # f | m
    rate: float = 1.0          # 1.0 = normal
    token: str = ""

VOICES = {("ar", "f"): "ar-SA-ZariyahNeural", ("ar", "m"): "ar-SA-HamedNeural",
          ("en", "f"): "en-US-AriaNeural", ("en", "m"): "en-US-GuyNeural"}

@app.post("/tts")
async def tts(req: TTSReq, bg: BackgroundTasks):
    check(req.token)
    text = req.text.strip()
    if not text: raise HTTPException(400, "empty text")
    lang = "ar" if re.search(r"[\u0600-\u06FF]", text) else "en"
    voice = req.voice or VOICES[(lang, "m" if req.gender == "m" else "f")]
    pct = int(round((req.rate - 1) * 100)); rate = f"{pct:+d}%"
    folder = tempfile.mkdtemp(); out = os.path.join(folder, "voice.mp3")
    await edge_tts.Communicate(text, voice, rate=rate).save(out)
    bg.add_task(cleanup, folder)
    return FileResponse(out, media_type="audio/mpeg")

@app.post("/cut")
async def cut(bg: BackgroundTasks, video: UploadFile = File(...), clips: str = Form(...), token: str = Form("")):
    """clips = JSON [{"start":15,"end":40}, ...] -> one merged mp4."""
    check(token)
    try: items = json.loads(clips)
    except Exception: raise HTTPException(400, "clips must be JSON")
    if not items: raise HTTPException(400, "no clips")
    folder = tempfile.mkdtemp(); bg.add_task(cleanup, folder)
    src = await save_upload(video, folder, "in.mp4"); parts = []
    for i, c in enumerate(items):
        s, e = float(c["start"]), float(c["end"])
        if e <= s: continue
        part = os.path.join(folder, f"p{i}.mp4")
        run(["ffmpeg", "-y", "-ss", str(s), "-to", str(e), "-i", src, "-c:v", "libx264", "-preset", "veryfast",
             "-c:a", "aac", "-pix_fmt", "yuv420p", part])
        parts.append(part)
    if not parts: raise HTTPException(400, "invalid clips")
    lst = os.path.join(folder, "list.txt")
    open(lst, "w").write("".join(f"file '{p}'\n" for p in parts))
    out = os.path.join(folder, "out.mp4")
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", out])
    return FileResponse(out, media_type="video/mp4", filename="clips.mp4")

@app.post("/dub")
async def dub(bg: BackgroundTasks, video: UploadFile = File(...), audio: UploadFile = File(...),
              keep_original: bool = Form(False), token: str = Form("")):
    """Replace (or mix with) the video's audio with a new voice file."""
    check(token)
    folder = tempfile.mkdtemp(); bg.add_task(cleanup, folder)
    v = await save_upload(video, folder, "v.mp4"); a = await save_upload(audio, folder, "a.mp3")
    out = os.path.join(folder, "out.mp4")
    if keep_original:
        run(["ffmpeg", "-y", "-i", v, "-i", a, "-filter_complex",
             "[0:a]volume=0.25[o];[o][1:a]amix=inputs=2:duration=first[m]", "-map", "0:v", "-map", "[m]",
             "-c:v", "copy", "-c:a", "aac", out])
    else:
        run(["ffmpeg", "-y", "-i", v, "-i", a, "-map", "0:v", "-map", "1:a", "-c:v", "copy",
             "-c:a", "aac", "-shortest", out])
    return FileResponse(out, media_type="video/mp4", filename="dubbed.mp4")
