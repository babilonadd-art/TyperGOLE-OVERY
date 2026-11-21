"""Prematch goalkeeper saves scanner."""
import json, re
from pathlib import Path
import bot as core

STATE_PATH=Path("data/prematch_saves_state.json")
MIN_ODDS,MAX_ODDS,MAX_DAILY_PICKS=1.60,8.00,4
LINES=(2.5,3.5,4.5,5.5)
SAMPLE_SIZE,MIN_SAMPLE,MIN_HIT_RATE=10,5,0.70
SAVE_MARKERS=("goalkeeper saves","goalie saves","saves by goalkeeper","obrony bramkarza","liczba obron bramkarza","bramkarz obrony")
HALF_MARKERS=("1 pol","pierwsza polowa","first half","1st half","2nd half","druga polowa","half time","ht")
EXTRA_MARKERS=("dogryw","extra time","overtime","penalty shoot","rzuty karne")

def load_state():
    STATE_PATH.parent.mkdir(parents=True,exist_ok=True)
    if STATE_PATH.exists():
        try:
            s=json.loads(STATE_PATH.read_text(encoding="utf-8"))
            if isinstance(s,dict):
                s.setdefault("sent",[]); return s
        except (OSError,ValueError): pass
    return {"sent":[]}

def save_state(s):
    STATE_PATH.parent.mkdir(parents=True,exist_ok=True)
    STATE_PATH.write_text(json.dumps(s,ensure_ascii=False,indent=2),encoding="utf-8")

def norm(v):
    v=str(v or "").replace("ł","l").replace("Ł","L")
    v=core.unicodedata.normalize("NFKD",v).encode("ascii","ignore").decode().lower()
    return re.sub(r"\s+"," ",re.sub(r"[^a-z0-9.,+-]+"," ",v)).strip()

def recent_keeper_rows(results,team,venue):
    found=[]
    for r in results:
        side="home" if core.team_name_similarity(team,str(r.get("home") or ""))>=0.90 else ("away" if core.team_name_similarity(team,str(r.get("away") or ""))>=0.90 else None)
        if side!=venue: continue
        sot=core.as_int(r.get("away_target" if side=="home" else "home_target"))
        conceded=core.as_int(r.get("ft_away" if side=="home" else "ft_home"))
        date=str(r.get("date") or "")
        if sot is None or conceded is None or not date or sot<conceded: continue
        found.append((date,sot-conceded,sot,conceded))
    found.sort(key=lambda x:x[0],reverse=True)
    return found[:SAMPLE_SIZE]

def analyze(home_rows,away_rows):
    picks=[]
    for side,keeper,opponent in (("home",home_rows,away_rows),("away",away_rows,home_rows)):
        n=min(len(keeper),len(opponent))
        if n<MIN_SAMPLE: continue
        saves=[x[1] for x in keeper[:n]]
        opp_sot=[x[2] for x in opponent[:n]]
        opp_goals=[x[3] for x in opponent[:n]]
        avg=sum(saves)/n
        expected=0.65*avg+0.35*max(0.0,sum(opp_sot)/n-sum(opp_goals)/n)
        rates={line:sum(v>line for v in saves)/n for line in LINES}
        valid=[line for line in LINES if rates[line]>=MIN_HIT_RATE and expected>=line+0.25]
        if valid:
            line=max(valid)
            picks.append({"team_side":side,"line":line,"expected":expected,"hit_rate":rates[line],"sample":n,"avg_saves":avg,"avg_opponent_sot":sum(opp_sot)/n})
    return picks

def collect_rows(node,inherited=""):
    out=[]
    if isinstance(node,list):
        for child in node: out.extend(collect_rows(child,inherited))
        return out
    if not isinstance(node,dict): return out
    keys=("marketName","market_name","marketGroupName","market_group_name","marketTitle","market_title","market","marketGroup","market_group","title","name","teamName","team_name","playerTeam","player_team","participantTeam","participant_team","competitorName","competitor_name")
    local=" ".join(str(node[k]) for k in keys if node.get(k) not in (None,"") and not isinstance(node.get(k),(dict,list)))
    context=" ".join(x for x in (inherited,local) if x).strip()
    price=any(node.get(k) not in (None,"") for k in ("price","odds","oddValue","odd_value","oddsValue","odds_dec"))
    selection=any(node.get(k) not in (None,"") for k in ("name","selectionName","selection_name","oddName","outcomeName","label","displayName"))
    if price and selection:
        row=dict(node); row["_market_context"]=context; out.append(row)
    for v in node.values():
        if isinstance(v,(dict,list)): out.extend(collect_rows(v,context))
    return out

def parse_quotes(detail,event):
    quotes=[]
    for row in collect_rows(detail):
        market=norm(row.get("_market_context"))
        if not any(m in market for m in SAVE_MARKERS) or any(m in market for m in HALF_MARKERS+EXTRA_MARKERS): continue
        status=str(core.first(row,"status","oddStatus","state",default="")).strip().lower()
        if status not in {"active","opened","open","1","true"}: continue
        selection=str(core.first(row,"name","selectionName","selection_name","oddName","outcomeName","label","displayName",default="")).strip()
        combined=norm(market+" "+selection)
        m=re.search(r"\b(?:powyzej|over)\s*(\d+(?:[.,]\d+)?)\b",combined)
        if not m: continue
        line=float(m.group(1).replace(",","."))
        if line not in LINES: continue
        side=None
        for candidate,team in (("home",event["home"]),("away",event["away"])):
            t=norm(team)
            if t and re.search(r"(?<![a-z0-9])"+re.escape(t)+r"(?![a-z0-9])",combined):
                side=candidate; break
        if side is None:
            if re.search(r"\b(?:gospodarze|home team|home goalkeeper|home)\b",combined): side="home"
            elif re.search(r"\b(?:goscie|away team|away goalkeeper|away)\b",combined): side="away"
        if side is None: continue
        prices=[]; bad=False
        for key in ("price","odds","oddValue","odd_value","oddsValue","odds_dec"):
            raw=row.get(key)
            if raw in (None,""): continue
            try: p=float(str(raw).replace(",","."))
            except (TypeError,ValueError): bad=True; break
            if not 1<p<=1000: bad=True; break
            prices.append(p)
        if bad or not prices or max(prices)-min(prices)>0.005: continue
        if MIN_ODDS<=prices[0]<=MAX_ODDS:
            quotes.append({"team_side":side,"line":line,"odds":prices[0],"selection":selection})
    unique={}
    for q in quotes: unique.setdefault((q["team_side"],q["line"]),set()).add(q["odds"])
    return [q for q in quotes if len(unique[(q["team_side"],q["line"])])==1]

def already_sent(state,day,event_id,side):
    key=f"{day}:{event_id}:{side}"
    return any(x.get("key")==key and x.get("delivery_status") in {"pending","sent"} for x in state["sent"])

def publish(state,event,analysis,quote,league):
    side=quote["team_side"]
    stats=next((x for x in analysis if x["team_side"]==side and x["line"]==quote["line"]),None)
    if not stats: return False
    kickoff=core.kickoff_datetime({"kickoff_time":event.get("time")})
    if not kickoff or kickoff<=core.now_local(): return False
    day=kickoff.astimezone(core.TZ).date().isoformat()
    event_id=str(event.get("event_id") or "")
    if not event_id or already_sent(state,day,event_id,side): return False
    daily=[x for x in state["sent"] if x.get("date")==day and x.get("delivery_status")=="sent"]
    if len(daily)>=MAX_DAILY_PICKS: return False
    url=str(event.get("url") or "")
    if not url.startswith("https://superbet.pl/"): return False
    team=event["home"] if side=="home" else event["away"]
    msg=("🧤 OBRONY BRAMKARZA — FT\n"
         f"🏆 {league}\n⚽ {event['home']} – {event['away']}\n"
         f"📌 Typ: bramkarz {team} — over {quote['line']:.1f} obron (FT)\n"
         f"📈 Kurs Superbet: {quote['odds']:.2f}\n"
         f"📊 Ostatnie {stats['sample']} meczów: over trafiony {round(stats['hit_rate']*stats['sample'])}/{stats['sample']}; średnia obron {stats['avg_saves']:.2f}; celne strzały rywali {stats['avg_opponent_sot']:.2f}\n"
         f"🕒 {kickoff.astimezone(core.TZ).strftime('%d.%m.%Y %H:%M')} (Polska)\n"
         f"🔗 Mecz Superbet: {url}\nŹródło: Open Matchday — celne strzały rywali minus stracone gole.")
    rec={"key":f"{day}:{event_id}:{side}","date":day,"event_id":event_id,"home":event["home"],"away":event["away"],"team":team,"team_side":side,"line":quote["line"],"odds":quote["odds"],"delivery_status":"pending","sent_at":core.now_local().isoformat()}
    state["sent"].append(rec); save_state(state)
    try:
        result=core.telegram(msg)
        payload=result.get("result",{}) if isinstance(result,dict) else {}
        rec["telegram_message_id"]=payload.get("message_id") if isinstance(payload,dict) else None
        rec["delivery_status"]="sent"; rec["sent_at"]=core.now_local().isoformat(); save_state(state)
        print(f"SAVES WYSŁANY: {team} over {quote['line']}, kurs {quote['odds']:.2f}",flush=True)
        return True
    except Exception as exc:
        rec["delivery_status"]="failed"; rec["delivery_error"]=f"{type(exc).__name__}: {exc}"; save_state(state)
        print(f"Saves Telegram error: {type(exc).__name__}: {exc}",flush=True); return False

def main():
    now=core.now_local()
    print(f"=== PREMATCH SAVES START {now.isoformat()} ===",flush=True)
    if not core.TELEGRAM_TOKEN or not core.CHAT_ID: raise RuntimeError("Brak sekretów TELEGRAM_BOT_TOKEN lub TELEGRAM_CHAT_ID")
    state=load_state()
    results=core.open_matchday_data().get("results",[])
    print(f"Open Matchday: {len(results)} wyników; saves = celne strzały przeciwko minus stracone gole.",flush=True)
    events=[]
    for day in (now.date(),now.date()+core.timedelta(days=1)):
        events.extend(core.superbet_prematch_event_links(target_date=day))
    unique={str(e.get("event_id")):e for e in events if e.get("event_id")}
    future=[]
    for e in unique.values():
        k=core.kickoff_datetime({"kickoff_time":e.get("time")})
        if k and k>now: future.append(e)
    print(f"Superbet: {len(future)} przyszłych meczów na dziś i noc.",flush=True)
    sent=examined=no_stats=no_market=0
    for event in future:
        if sent>=MAX_DAILY_PICKS: break
        examined+=1
        home_rows=recent_keeper_rows(results,event["home"],"home")
        away_rows=recent_keeper_rows(results,event["away"],"away")
        if min(len(home_rows),len(away_rows))<MIN_SAMPLE:
            no_stats+=1; continue
        analysis=analyze(home_rows,away_rows)
        if not analysis:
            no_stats+=1; continue
        try: detail=core.superbet_event_detail(event["event_id"])
        except Exception as exc:
            print(f"Superbet detail {event['home']} - {event['away']}: {type(exc).__name__}: {exc}",flush=True); no_market+=1; continue
        quotes=parse_quotes(detail,event)
        if not quotes:
            no_market+=1; continue
        league=event.get("competition_name") or "Dane Open Matchday"
        for quote in quotes:
            if sent>=MAX_DAILY_PICKS: break
            if publish(state,event,analysis,quote,league): sent+=1
    print(f"PREMATCH SAVES: sprawdzono={examined}, bez_statystyk={no_stats}, bez_potwierdzonego_rynku={no_market}, wysłano={sent}.",flush=True)

if __name__=="__main__": main()
