"""Opt-in cloud transcription with the meeting's stable local speaker timeline."""
from concurrent.futures import ThreadPoolExecutor
import base64
import hashlib
import json
import math
import os
from pathlib import Path
import urllib.error
import urllib.request

from .config import openrouter_key, meeting_language

ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
VERSION = 1
RESERVE = .05
PROMPT = ("Transcribe this meeting audio verbatim in its original language, preserving Russian/English switches. "
          "Do not translate, summarize, fix the speaker's ideas, or invent words. "
          "The supplied turns are an approximate local speaker timeline in seconds relative to this audio. "
          "Listen to the whole audio for context, then return the speech belonging to EACH supplied turn id. "
          "Return every id exactly once, in order, with its text; use an empty string for noise/silence. "
          "Do not move text between speakers or duplicate words across adjacent turns. "
          "Do not identify people or invent names. Return JSON matching the supplied schema.")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise RuntimeError("OpenRouter redirected; audio was kept and no request was repeated.")


def _cost(receipt):
    value = receipt.get("usage", {}).get("cost")
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise RuntimeError("OpenRouter did not report cost. Inspect the saved receipt before retrying.")
    return value


def _decode(receipt, turns):
    if receipt.get("finish_reason") != "stop":
        raise RuntimeError("OpenRouter returned an incomplete response (" + str(receipt.get("finish_reason")) + "). Completed windows were kept; retry explicitly.")
    try:
        value = json.loads(receipt["text"])
        rows = value["turns"]
        if not isinstance(rows, list) or len(rows) != len(turns): raise ValueError()
        for row, turn in zip(rows, turns):
            if not isinstance(row, dict) or row.get("id") != turn["id"] or not isinstance(row.get("text"), str):
                raise ValueError()
    except (KeyError, TypeError, ValueError) as error:
        raise RuntimeError("OpenRouter returned invalid turn alignment. Completed windows were kept; retry explicitly.") from error
    return rows


def _request(raw, turns, config):
    model = config.get("KWISPR_MEETING_CLOUD_MODEL", "google/gemini-3.8-flash")
    context = config.get("KWISPR_VOCABULARY", "")[:4096]
    prompt = PROMPT + "\nLanguage hint (preserve switches, do not translate): " + config.get("_cloud_language", "auto") + "\nPersonal vocabulary spellings (not speech): " + context + "\nTurns: " + json.dumps(turns, ensure_ascii=False)
    schema = {"type":"object", "properties":{"turns":{"type":"array", "items":{"type":"object",
        "properties":{"id":{"type":"string"},"text":{"type":"string"}},
        "required":["id","text"],"additionalProperties":False}}},
        "required":["turns"],"additionalProperties":False}
    body = {"model":model,"temperature":0,"max_tokens":8192,"reasoning":{"effort":"minimal"},
        "response_format":{"type":"json_schema","json_schema":{"name":"meeting_turns","strict":True,"schema":schema}},
        "messages":[{"role":"user","content":[{"type":"text","text":prompt},
        {"type":"input_audio","input_audio":{"data":base64.b64encode(raw).decode(),"format":"wav"}}]}]}
    request = urllib.request.Request(ENDPOINT, data=json.dumps(body).encode(),
        headers={"Authorization":"Bearer "+openrouter_key(config),"Content-Type":"application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
    with opener.open(request,timeout=90) as response:
        result = json.loads(response.read(2*1024*1024))
    choice = result["choices"][0]
    return {"text":choice["message"].get("content") or "", "finish_reason":choice.get("finish_reason"),
            "usage":result.get("usage") or {},"generation_id":result.get("id")}


def _window(directory, item, config, token):
    from .pipeline import atomic_json, read_json
    path = directory/(item["identity"]+".json")
    marker = path.with_suffix(".attempt")
    receipt = read_json(path,{})
    if receipt:
        if receipt.get("identity") != item["identity"]: raise RuntimeError("OpenRouter cache identity mismatch.")
        _cost(receipt)
        try:
            return _decode(receipt,item["turns"])
        except RuntimeError:
            if receipt.get("run_token") == token: raise
            # A user-requested retry may repeat a known failed response. Unknown
            # network outcomes retain their marker and are never resent blindly.
    if marker.exists():
        raise RuntimeError("An OpenRouter request has an unknown outcome. Saved audio and completed windows were kept; inspect its attempt marker before retrying.")
    # Keep every billed response, including failed attempts, in the cost ledger.
    if receipt:
        history = directory/(item["identity"]+"-"+hashlib.sha256(json.dumps(receipt,sort_keys=True).encode()).hexdigest()[:12]+".receipt")
        path.replace(history)
    fd = os.open(marker,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,"w") as stream:
        json.dump({"identity":item["identity"],"run_token":token},stream)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        receipt = _request(item["raw"],item["turns"],config)
    except urllib.error.HTTPError as error:
        code = error.code
        error.close()
        marker.unlink()
        raise RuntimeError(f"OpenRouter returned HTTP {code}. Audio was kept; check key, credits and model.") from None
    except Exception:
        raise RuntimeError("OpenRouter request outcome is unknown. Audio and its attempt marker were kept; no automatic retry.") from None
    receipt.update(run_token=token,identity=item["identity"])
    atomic_json(path,receipt)
    marker.unlink()
    _cost(receipt)
    return _decode(receipt,item["turns"])


def spent(directory):
    from .pipeline import read_json
    return sum(_cost(read_json(path,{})) for path in [*directory.glob('*.json'),*directory.glob('*.receipt')])


def transcribe_track(session_dir,audio,intervals,config,track,offset,token,progress=None):
    from .pipeline import _wav_slice, read_json
    root = Path(session_dir)/"openrouter"
    root.mkdir(mode=0o700,exist_ok=True)
    if any(root.glob("*.attempt")):
        raise RuntimeError("An OpenRouter request has an unknown outcome. Inspect its saved attempt marker before retrying; no new audio was sent.")
    model = config.get("KWISPR_MEETING_CLOUD_MODEL","google/gemini-3.8-flash")
    language = meeting_language(config,track)
    config = dict(config, _cloud_language=language or "auto")
    jobs = []
    duration = len(audio)/16000
    for first in range(0,len(audio),45*16000):
        start,end = first/16000,min(duration,(first+45*16000)/16000)
        turns,targets = [],[]
        for i,interval in enumerate(intervals):
            left,right = max(start,interval['start']),min(end,interval['end'])
            if right<=left: continue
            turn = {"id":str(i),"start":round(left-start,3),"end":round(right-start,3),"speakers":interval['speakers']}
            turns.append(turn)
            targets.append({"start":round(left+offset,3),"end":round(right+offset,3),"speakers":interval['speakers'],"track":track,
                            "speaker_grouped":interval.get('speaker_grouped',False)})
        if not turns: continue
        raw = _wav_slice(audio,start,end)
        identity = hashlib.sha256(json.dumps({"version":VERSION,"model":model,"prompt":PROMPT,
            "vocabulary":config.get('KWISPR_VOCABULARY','')[:4096],"track":track,"turns":turns,"language":language,
            "audio_sha256":hashlib.sha256(raw).hexdigest()},sort_keys=True).encode()).hexdigest()
        jobs.append({"identity":identity,"turns":turns,"targets":targets,"raw":raw})
    budget = float(config.get('KWISPR_MEETING_CLOUD_BUDGET_USD','1.5'))
    rows = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        for first in range(0,len(jobs),4):
            batch = jobs[first:first+4]
            missing = 0
            for job in batch:
                receipt = read_json(root/(job['identity']+'.json'), {})
                try:
                    _decode(receipt,job['turns'])
                except RuntimeError:
                    missing += 1
            if missing and spent(root)+RESERVE*missing>budget:
                raise RuntimeError("Meeting cloud budget reached. Completed windows were kept; increase the budget to continue.")
            futures = [pool.submit(_window,root,job,config,token) for job in batch]
            failure = None
            for job,future in zip(batch,futures):
                try:
                    answers = future.result()
                    rows.extend({**target,"text":answer['text'].strip(),"requested_language":language or 'auto',
                                 "language_mode":"cloud-context"} for target,answer in zip(job['targets'],answers))
                except Exception as error:
                    failure = error
            if failure: raise failure
            if progress: progress(f"Transcribing with OpenRouter: {min(first+4,len(jobs))}/{len(jobs)} windows · ${spent(root):.3f}")
    return rows,{"requested":language or 'auto',"backend":"openrouter","model":model}
