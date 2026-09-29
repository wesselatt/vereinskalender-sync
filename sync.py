"""TSV ICS-Sync 0.5.3: ausschließlich freigegebene Daten, keine Admin-Anmeldung.

Die vorhandenen Firestore-Regeln bleiben unverändert. Netzfunktionen sind für
lokale Tests injizierbar. Import dieses Moduls startet keinen Netzabruf.
"""
from __future__ import annotations

import datetime as dt
import email.utils
import hashlib
import json
import os
from pathlib import Path
import random
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

VERSION = "0.5.3"
PROJECT = "vereinskalender-tsv-aw"
API = f"https://firestore.googleapis.com/v1/projects/{PROJECT}/databases/(default)/documents"
USER_AGENT = "TSV-Vereinskalender/0.5.3 (+https://vereinskalender-tsv-aw.web.app)"
BERLIN = ZoneInfo("Europe/Berlin")
UTC = dt.timezone.utc


def utcnow():
    return dt.datetime.now(UTC).isoformat()


def atomic_json(path, value):
    atomic_bytes(path, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def atomic_bytes(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tsv-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(value)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def scalar(value):
    if "stringValue" in value:
        return value["stringValue"]
    if "booleanValue" in value:
        return value["booleanValue"]
    if "integerValue" in value:
        return int(value["integerValue"])
    if "doubleValue" in value:
        return float(value["doubleValue"])
    if "timestampValue" in value:
        return value["timestampValue"]
    if "arrayValue" in value:
        return [scalar(x) for x in value["arrayValue"].get("values", [])]
    if "mapValue" in value:
        return {k: scalar(v) for k, v in value["mapValue"].get("fields", {}).items()}
    return None


def document(raw):
    result = {k: scalar(v) for k, v in raw.get("fields", {}).items()}
    result["id"] = raw["name"].rsplit("/", 1)[-1]
    return result


class RemoteError(RuntimeError):
    def __init__(self, label, code, status="", message=""):
        self.code = code
        self.status = status
        detail = f" – {status}" if status else ""
        # Keine Servertexte mit URLs, Auth-Daten oder Quellinhalten protokollieren.
        hint = {
            403: "Zugriff verweigert; kein erneuter Versuch. Abfrage und Freigabe prüfen.",
            429: "Abrufkontingent oder Anfragerate begrenzt; Ursache nicht allein aus HTTP 429 bestimmbar.",
            400: "Abfrage ungültig oder erforderlicher Index fehlt; Konfiguration prüfen.",
        }.get(code, "Abruf fehlgeschlagen.")
        super().__init__(f"{label}: HTTP {code}{detail}. {hint}")


def wait_seconds(headers, attempt, now=None):
    """Retry-After niemals verkürzen. Zu lange Wartezeiten werden nicht abgewartet."""
    raw = headers.get("Retry-After") if headers else None
    if raw:
        try:
            return max(0, float(raw))
        except (TypeError, ValueError):
            try:
                target = email.utils.parsedate_to_datetime(raw)
                if target.tzinfo is None:
                    target = target.replace(tzinfo=UTC)
                return max(0, (target - (now or dt.datetime.now(UTC))).total_seconds())
            except (TypeError, ValueError, OverflowError):
                pass
    return (20, 45)[min(attempt, 1)]


class Firestore:
    def __init__(self, opener=urllib.request.urlopen, sleep=time.sleep, log=print, base=API):
        self.opener, self.sleep, self.log, self.base = opener, sleep, log, base
        self.cache = {}

    def request(self, url, label, body=None):
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        payload = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            payload = json.dumps(body).encode("utf-8")
        for attempt in range(3):
            req = urllib.request.Request(url, data=payload, headers=headers)
            try:
                with self.opener(req, timeout=45) as res:
                    return json.load(res)
            except urllib.error.HTTPError as exc:
                try:
                    detail = json.loads(exc.read(65536)).get("error", {})
                except (ValueError, OSError):
                    detail = {}
                err = RemoteError(label, exc.code, str(detail.get("status", "")))
                if exc.code not in (429, 500, 502, 503, 504) or attempt == 2:
                    raise err from None
                delay = wait_seconds(exc.headers, attempt)
                if delay > 180:
                    self.log(f"{label}: Server verlangt {int(delay)} s Pause; kein vorzeitiger Wiederholungsversuch.")
                    raise err from None
                self.log(f"WARTE {label}: HTTP {exc.code}, {int(delay)} s, Versuch {attempt + 2}/3 …")
                self.sleep(delay)
            except (urllib.error.URLError, TimeoutError) as exc:
                raise RuntimeError(f"{label}: Netzwerkverbindung fehlgeschlagen ({type(exc).__name__}).") from None
        raise RuntimeError("Nicht erreichbarer Zustand")

    def sync_sources(self):
        """Nur diese bereits öffentliche technische Collection darf gelistet werden."""
        out, token, visited = [], None, set()
        while True:
            params = {"pageSize": 500}
            if token:
                params["pageToken"] = token
            raw = self.request(self.base + "/publicSyncSources?" + urllib.parse.urlencode(params), "Firebase Quellenliste")
            out.extend(document(d) for d in raw.get("documents", []))
            token = raw.get("nextPageToken")
            if not token:
                return [s for s in out if s.get("active", True) is True]
            if token in visited:
                raise RuntimeError("Firebase Quellenliste: wiederholter Seitenschlüssel.")
            visited.add(token)

    def query(self, collection, filters):
        key = (collection, tuple(filters))
        if key in self.cache:
            return self.cache[key]
        # Verhindert auch versehentliche spätere unbeschränkte REST-Listen.
        if collection == "calendars":
            if filters not in ([("publishIcs", True)], [("publishHomepage", True)]):
                raise ValueError("Kalenderabfrage benötigt eine ausdrückliche Veröffentlichungsfreigabe.")
        elif collection == "events":
            f = dict(filters)
            if f.get("visibility") != "public" or not f.get("calendarId") or len(filters) != 2:
                raise ValueError("Terminabfrage benötigt Kalender-ID UND visibility=public.")
        else:
            raise ValueError("Diese Sammlung wird vom öffentlichen Export nicht gelesen.")
        ff = [{"fieldFilter": {"field": {"fieldPath": k}, "op": "EQUAL", "value":
               {"booleanValue": v} if isinstance(v, bool) else {"stringValue": v}}} for k, v in filters]
        where = ff[0] if len(ff) == 1 else {"compositeFilter": {"op": "AND", "filters": ff}}
        out, cursor, seen = [], None, set()
        while True:
            query = {"from": [{"collectionId": collection}], "where": where,
                     "orderBy": [{"field": {"fieldPath": "__name__"}, "direction": "ASCENDING"}], "limit": 500}
            if cursor:
                query["startAt"] = {"values": [{"referenceValue": cursor}], "before": False}
            raw = self.request(self.base + ":runQuery", "Firebase veröffentlichte " + collection, {"structuredQuery": query})
            if not isinstance(raw, list):
                raise RuntimeError("Firebase: unerwartetes Abfrageformat.")
            docs = [row["document"] for row in raw if "document" in row]
            for d in docs:
                if d["name"] not in seen:
                    out.append(document(d)); seen.add(d["name"])
            if len(docs) < 500:
                break
            next_cursor = docs[-1]["name"]
            if cursor == next_cursor:
                raise RuntimeError("Firebase: Seitenumbruch konnte nicht fortgesetzt werden.")
            cursor = next_cursor
        self.cache[key] = out
        return out

    def public_calendars(self):
        records = self.query("calendars", [("publishIcs", True)]) + self.query("calendars", [("publishHomepage", True)])
        return {c["id"]: c for c in records if c.get("publishIcs") is True or c.get("publishHomepage") is True}

    def public_events(self, calendar_id):
        return self.query("events", [("calendarId", calendar_id), ("visibility", "public")])


def source_url(url):
    url = str(url or "").strip()
    if url.lower().startswith("webcal://"):
        url = "https://" + url[9:]
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise ValueError("ICS-Quelle benötigt eine HTTPS-/webcal-Adresse ohne eingebettetes Passwort.")
    return url


def fetch_ics(url, name, opener=urllib.request.urlopen, sleep=time.sleep, log=print):
    url = source_url(url)
    for attempt in range(3):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/calendar,text/plain;q=0.9"})
        try:
            with opener(req, timeout=60) as res:
                raw = res.read(8 * 1024 * 1024 + 1)
                if len(raw) > 8 * 1024 * 1024:
                    raise ValueError("ICS-Datei überschreitet 8 MiB.")
                return raw.decode("utf-8-sig", "strict")
        except urllib.error.HTTPError as exc:
            if exc.code not in (429, 500, 502, 503, 504) or attempt == 2:
                raise RemoteError("ICS-Quelle " + name, exc.code) from None
            delay = wait_seconds(exc.headers, attempt)
            if delay > 180:
                raise RemoteError("ICS-Quelle " + name, exc.code) from None
            log(f"WARTE Quelle {name}: HTTP {exc.code}, {int(delay)} s, Versuch {attempt + 2}/3 …")
            sleep(delay)


def unescape(value):
    return re.sub(r"\\([nN,;\\])", lambda m: "\n" if m[1] in "nN" else m[1], value)


def parse_stamp(value, params=""):
    if re.fullmatch(r"\d{8}", value):
        return dt.datetime.strptime(value, "%Y%m%d").date()
    is_utc = value.endswith("Z")
    value = value.rstrip("Z")
    result = dt.datetime.strptime(value, "%Y%m%dT%H%M%S" if len(value) == 15 else "%Y%m%dT%H%M")
    match = re.search(r'(?:^|;)TZID=("[^"]+"|[^;]+)', params, flags=re.I)
    zone = UTC if is_utc else ZoneInfo(match[1].strip('"')) if match else BERLIN
    return result.replace(tzinfo=zone).astimezone(BERLIN)


def parse_feed(body, source):
    lines = re.sub(r"\r?\n[ \t]", "", body.replace("\r\n", "\n")).splitlines()
    if "BEGIN:VCALENDAR" not in lines or "END:VCALENDAR" not in lines:
        raise ValueError("Die Antwort ist kein vollständiger ICS-Kalender.")
    events, current, nested = [], None, 0
    for line in lines:
        if line == "BEGIN:VEVENT":
            if current is not None:
                raise ValueError("Verschachtelter VEVENT-Block.")
            current, nested = {}, 0
        elif line == "END:VEVENT" and current is not None:
            if current.get("STATUS", ("", ""))[0] != "CANCELLED":
                if "RRULE" in current:
                    # Keine falsche Einzelinstanz aus einer Serie erzeugen.
                    raise ValueError("Dieser automatische Quellimport enthält RRULE-Serien. Bitte dafür den Google-/ICS-Import der WebApp verwenden.")
                if "UID" not in current or "DTSTART" not in current:
                    raise ValueError("ICS-Termin ohne UID oder DTSTART.")
                start = parse_stamp(*current["DTSTART"])
                end = parse_stamp(*current["DTEND"]) if "DTEND" in current else None
                allday = not isinstance(start, dt.datetime)
                uid = unescape(current["UID"][0])
                e = {"id": "ext-" + source["id"] + "-" + uid, "externalUid": uid,
                     "title": str(source.get("prefix") or "") + unescape(current.get("SUMMARY", ("Termin", ""))[0]) + str(source.get("suffix") or ""),
                     "description": unescape(current.get("DESCRIPTION", ("", ""))[0]),
                     "location": unescape(current.get("LOCATION", ("", ""))[0]),
                     "date": start.isoformat() if allday else start.date().isoformat(),
                     "time": "" if allday else start.strftime("%H:%M"),
                     "endTime": end.strftime("%H:%M") if isinstance(end, dt.datetime) else "",
                     "allDay": allday, "visibility": source.get("visibility"),
                     "calendarId": source.get("calendarId", ""), "externalSourceId": source["id"], "external": True}
                if end is not None:
                    e["endDate"] = end.date().isoformat() if isinstance(end, dt.datetime) else end.isoformat()
                events.append(e)
            current = None
        elif current is not None:
            if line.startswith("BEGIN:"):
                nested += 1
            elif line.startswith("END:"):
                nested -= 1
            elif nested == 0 and ":" in line:
                key, value = line.split(":", 1)
                name, _, params = key.partition(";")
                current[name.upper()] = (value, params)
    if current is not None:
        raise ValueError("Nicht abgeschlossener VEVENT-Block.")
    return events


def clean_event(event):
    # Nur Termine, die ausdrücklich öffentlich markiert wurden.
    if event.get("visibility") != "public":
        return None
    fields = ("id", "externalUid", "externalSourceId", "title", "description", "location", "date", "time", "endTime", "endDate", "allDay", "calendarId", "external")
    result = {k: event[k] for k in fields if k in event}
    result["visibility"] = "public"
    return result


def source_ids(calendar, calendars, trail=()):
    cid = calendar["id"]
    if cid in trail:
        raise ValueError("Sammelkalender enthält einen Kreisverweis.")
    if calendar.get("type", "source") != "aggregate":
        return [cid]
    out = []
    for entry in calendar.get("sources", []):
        sid = entry if isinstance(entry, str) else entry.get("id", "")
        themes = calendar.get("sourceVisibility", {}).get(sid, ["public"])
        if "public" not in themes:
            continue
        if sid not in calendars:
            raise ValueError("Ein eingebundener Quellkalender ist nicht öffentlich freigegeben. Keine privaten Quelldaten werden exportiert.")
        out.extend(source_ids(calendars[sid], calendars, trail + (cid,)))
    return list(dict.fromkeys(out))


def ics_escape(value):
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    return text.replace("\\", "\\\\").replace("\n", "\\n").replace(";", "\\;").replace(",", "\\,")


def fold_line(value):
    lines, current, size = [], "", 0
    for char in value:
        n = len(char.encode("utf-8"))
        if size + n > 75:
            lines.append(current); current, size = " ", 1
        current += char; size += n
    lines.append(current)
    return "\r\n".join(lines)


def event_key(e):
    if e.get("externalUid"):
        return (e.get("externalSourceId") or e.get("calendarId"), e["externalUid"], e.get("date"), e.get("time"))
    return (e.get("calendarId"), e.get("id"))


def calendar_bytes(calendar, items, stamp=None):
    stamp = stamp or dt.datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//TSV Aue-Wingeshausen//Vereinskalender 0.5.3//DE",
             "CALSCALE:GREGORIAN", "METHOD:PUBLISH", "X-WR-CALNAME:" + ics_escape(calendar.get("name", "Kalender")), "X-WR-TIMEZONE:Europe/Berlin"]
    unique = {event_key(e): e for e in items if e.get("visibility") == "public"}
    for e in sorted(unique.values(), key=lambda x: (x.get("date", ""), x.get("time", ""), x.get("title", ""))):
        day = dt.date.fromisoformat(e["date"])
        if not e.get("id"):
            raise ValueError("Termin ohne stabile ID; Ausgabe nicht überschrieben.")
        # Jede gespeicherte Serieninstanz hat eine eigene, unveränderliche ID.
        # Externe Einzeltermine behalten ihre ID bei einer Spielverlegung.
        ident = e.get("externalSourceId", "") + "|" + str(e["id"])
        uid = hashlib.sha256(ident.encode()).hexdigest()[:40] + "@vereinskalender-tsv-aw"
        lines.extend(["BEGIN:VEVENT", "UID:" + uid, "DTSTAMP:" + stamp])
        if e.get("allDay") or not e.get("time"):
            end = dt.date.fromisoformat(e["endDate"]) if e.get("endDate") else day + dt.timedelta(days=1)
            if end <= day:
                raise ValueError("Ungültiges Ganztages-Enddatum.")
            lines.extend(["DTSTART;VALUE=DATE:" + day.strftime("%Y%m%d"), "DTEND;VALUE=DATE:" + end.strftime("%Y%m%d")])
        else:
            start = dt.datetime.combine(day, dt.time.fromisoformat(e["time"]), BERLIN)
            lines.append("DTSTART:" + start.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ"))
            if e.get("endTime"):
                end_day = dt.date.fromisoformat(e["endDate"]) if e.get("endDate") else day
                end = dt.datetime.combine(end_day, dt.time.fromisoformat(e["endTime"]), BERLIN)
                if end <= start and not e.get("endDate"):
                    end += dt.timedelta(days=1)
                if end <= start:
                    raise ValueError("Terminende liegt nicht nach dem Beginn.")
                lines.append("DTEND:" + end.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ"))
        lines.append("SUMMARY:" + ics_escape(e.get("title", "Termin")))
        if e.get("description"):
            lines.append("DESCRIPTION:" + ics_escape(e["description"]))
        if e.get("location"):
            lines.append("LOCATION:" + ics_escape(e["location"]))
        lines.extend(["STATUS:CONFIRMED", "END:VEVENT"])
    lines.append("END:VCALENDAR")
    return ("\r\n".join(fold_line(x) for x in lines) + "\r\n").encode("utf-8"), len(unique)


def run(fs=None, fetch=fetch_ics, output=Path("generated"), sleep=time.sleep, log=print):
    fs = fs or Firestore(log=log)
    output = Path(output)
    now = utcnow()
    summary = {"version": VERSION, "generatedAt": now, "sourceConfigMode": "firebase", "sources": [], "ics": {"state": "pending", "calendars": []}}
    source_cache = output / "sync-sources.json"
    try:
        try:
            sources = fs.sync_sources()
        except RemoteError as exc:
            if exc.code not in (429, 500, 502, 503, 504):
                raise
            cached = load_json(source_cache, {})
            saved = dt.datetime.fromisoformat(cached.get("savedAt", ""))
            if (dt.datetime.now(UTC) - saved).total_seconds() > 86400 or not isinstance(cached.get("sources"), list):
                raise RuntimeError("Quellenkonfiguration fehlt oder ist älter als 24 Stunden.") from None
            sources = cached["sources"]
            summary["sourceConfigMode"] = "cache"
        calendars = fs.public_calendars()
    except Exception as exc:
        message = str(exc)
        summary.update({"overall": "error", "configurationError": message, "dataUnchanged": True})
        summary["ics"]["state"] = "error"
        atomic_json(output / "status.json", summary)
        log("FEHLER Konfiguration: " + message)
        log("Keine Daten-/ICS-Datei wurde als aktuell neu veröffentlicht. Vorhandene Dateien bleiben unverändert.")
        return 3

    sources = [s for s in sources if s.get("active", True) is True]
    permitted = [s for s in sources if s.get("visibility") == "public" and s.get("calendarId") in calendars]
    summary["sourceCount"] = len(permitted)
    summary["excludedNonPublicSources"] = len(sources) - len(permitted)
    log(f"Öffentlich freigegebene aktive Quellen: {len(permitted)} (Konfiguration: {summary['sourceConfigMode']})")
    if summary["excludedNonPublicSources"]:
        log("Nicht öffentlich freigegebene Quellen werden nicht in das öffentliche GitHub-Repository übernommen.")
    if summary["sourceConfigMode"] == "firebase":
        keys = ("id", "name", "url", "calendarId", "visibility", "prefix", "suffix", "active")
        atomic_json(source_cache, {"savedAt": now, "sources": [{k: s[k] for k in keys if k in s} for s in permitted]})
    old = load_json(output / "external-events.json", {})
    previous = old.get("events", [])
    external = []
    for i, source in enumerate(permitted):
        if i:
            pause = random.randint(15, 25); log(f"Pause zwischen Quellen: {pause}s …"); sleep(pause)
        name = source.get("name", "Quelle")
        result = {"id": source["id"], "name": name, "calendarId": source["calendarId"], "ok": False, "stale": False}
        try:
            items = parse_feed(fetch(source["url"], name), source)
            external.extend(e for raw in items if (e := clean_event(raw)) is not None)
            result.update({"ok": True, "count": len(items), "lastSuccessAt": now})
            log(f"OK {name}: {len(items)} Termine")
        except Exception as exc:
            kept = [e for raw in previous if raw.get("externalSourceId") == source["id"] and raw.get("calendarId") == source["calendarId"] and (e := clean_event(raw)) is not None]
            external.extend(kept)
            result.update({"count": len(kept), "stale": bool(kept), "error": str(exc)})
            log(f"FEHLER {name}: {type(exc).__name__}; {len(kept)} bisherige öffentliche Termine bleiben erhalten.")
        summary["sources"].append(result)
    summary["eventCount"] = len(external)
    atomic_json(output / "external-events.json", {"generatedAt": now, "events": external, "sources": summary["sources"]})

    public_ics = {cid: c for cid, c in calendars.items() if c.get("publishIcs") is True}
    errors, manifest = [], []
    previous_manifest = load_json(output / "calendars.json", {}).get("calendars", [])
    old_manifest = {x["id"]: x for x in previous_manifest if "id" in x}
    for cid, cal in sorted(public_ics.items()):
        try:
            if not re.fullmatch(r"[A-Za-z0-9_-]+", cid):
                raise ValueError("Kalender-ID nicht als Dateiname geeignet.")
            ids = source_ids(cal, calendars)
            items = [e for e in external if e.get("calendarId") in ids and e.get("visibility") == "public"]
            for sid in ids:
                # Keine Collection-weite Terminliste! Leseregeln werden im Request erfüllt.
                items.extend(e for raw in fs.public_events(sid) if raw.get("calendarId") == sid and (e := clean_event(raw)) is not None)
            payload, count = calendar_bytes(cal, items)
            atomic_bytes(output / "calendars" / (cid + ".ics"), payload)
            # Kontrolle der tatsächlich geschriebenen Bytes, nicht nur der Dateiexistenz.
            if (output / "calendars" / (cid + ".ics")).read_bytes() != payload:
                raise OSError("Prüfung der geschriebenen ICS-Datei fehlgeschlagen.")
            state = "stale" if any(not x["ok"] and x["calendarId"] in ids for x in summary["sources"]) else "ok"
            entry = {"id": cid, "name": cal.get("name", cid), "path": f"generated/calendars/{cid}.ics", "eventCount": count,
                     "generatedAt": now, "state": state, "sha256": hashlib.sha256(payload).hexdigest()}
            manifest.append(entry)
            log(f"ICS {state.upper()} {entry['name']}: {count} Termine -> {entry['path']}")
        except Exception as exc:
            errors.append({"id": cid, "error": str(exc)})
            entry = dict(old_manifest.get(cid, {"id": cid, "name": cal.get("name", cid)}))
            entry.update({"state": "error", "lastAttemptAt": now, "error": str(exc)})
            manifest.append(entry)
            log(f"FEHLER ICS {cal.get('name', cid)}: {exc}. Eine bestehende Datei wird nicht durch eine leere ersetzt.")
    # Nur nach erfolgreicher aktueller Kalender-Abfrage: widerrufene Abos entfernen.
    revoked = 0
    for p in (output / "calendars").glob("*.ics"):
        if p.stem not in public_ics:
            p.unlink(); revoked += 1
    failed_sources = sum(not x["ok"] for x in summary["sources"])
    summary["ics"] = {"state": "error" if errors else "stale" if failed_sources else "ok", "calendars": manifest, "errors": errors, "revokedFilesRemoved": revoked}
    summary["overall"] = "error" if errors else "partial" if failed_sources else "ok"
    atomic_json(output / "calendars.json", {"generatedAt": now, "state": summary["ics"]["state"], "calendars": manifest})
    atomic_json(output / "status.json", summary)
    log(f"Eingang: {len(permitted) - failed_sources}/{len(permitted)} Quellen aktuell erfolgreich; {len(external)} öffentliche Termine verfügbar.")
    log(f"Ausgang: {len(public_ics) - len(errors)}/{len(public_ics)} ICS-Dateien erzeugt; Fehler: {len(errors)}.")
    if not public_ics:
        log("Kein Kalender mit publishIcs=true freigegeben. Kein Abolink verfügbar.")
    if errors:
        return 4
    return 2 if failed_sources else 0


if __name__ == "__main__":
    try:
        sys.exit(run(log=lambda message: print(message, flush=True)))
    except Exception as error:
        print("FEHLER: " + str(error), flush=True)
        sys.exit(1)
