set -Eeuo pipefail
INGRESS=/home/bking/AI/chatgpt-job-ingress/ingress.py
SERVICE=chatgpt-job-ingress.service

cp -a "$INGRESS" "$INGRESS.pre-v2.11-$(date +%Y%m%d-%H%M%S)"

python3 - "$INGRESS" <<'PY'
from pathlib import Path
import sys

p=Path(sys.argv[1])
s=p.read_text()

def once(old,new,label):
    global s
    if old not in s:
        raise SystemExit(f"missing patch anchor: {label}")
    if s.count(old)!=1:
        raise SystemExit(f"non-unique patch anchor {label}: {s.count(old)}")
    s=s.replace(old,new,1)

once(
'''WORK=ROOT/"repo"
LOCK=threading.Lock()
''',
'''WORK=ROOT/"repo"
WAKE_QUEUE=ROOT/"wake-queue"
WAKE_QUEUE.mkdir(parents=True,exist_ok=True)
LOCK=threading.Lock()
''',
"wake queue globals"
)

once(
'''  const ENDPOINT = "http://127.0.0.1:{port}/job";
  const BEGIN = "<<<AI_BEAST_AUTOMATION_JOB_V1_BEGIN>>>";
  const END = "<<<AI_BEAST_AUTOMATION_JOB_V1_END>>>";
  const BRIDGE_VERSION = "2.10";
''',
'''  const ENDPOINT = "http://127.0.0.1:{port}/job";
  const WAKE_NEXT_ENDPOINT = "http://127.0.0.1:{port}/wake-next";
  const WAKE_ACK_ENDPOINT = "http://127.0.0.1:{port}/wake-ack";
  const BEGIN = "<<<AI_BEAST_AUTOMATION_JOB_V1_BEGIN>>>";
  const END = "<<<AI_BEAST_AUTOMATION_JOB_V1_END>>>";
  const BRIDGE_VERSION = "2.11";
''',
"userscript endpoints/version"
)

once(
'''  async function sendAutoContinue() {{
    const composer=composerNode();
    if (!composer) return {{ok:false,reason:"composer not found"}};
    if (composerText(composer)) return {{ok:false,reason:"composer not empty"}};

    setComposerText(composer,AUTO_CONTINUE_TEXT);

    for (let attempt=0;attempt<20;attempt++) {{
      await sleep(100);
      const send=findSendButton();
      if (send) {{
        send.click();
        return {{ok:true,reason:""}};
      }}
    }}

    setComposerText(composer,"");
    return {{ok:false,reason:"send button unavailable"}};
  }}
''',
'''  async function sendComposerMessage(text) {{
    const composer=composerNode();
    if (!composer) return {{ok:false,reason:"composer not found"}};
    if (composerText(composer)) return {{ok:false,reason:"composer not empty"}};

    setComposerText(composer,String(text || ""));

    for (let attempt=0;attempt<20;attempt++) {{
      await sleep(100);
      const send=findSendButton();
      if (send) {{
        send.click();
        return {{ok:true,reason:""}};
      }}
    }}

    setComposerText(composer,"");
    return {{ok:false,reason:"send button unavailable"}};
  }}

  async function sendAutoContinue() {{
    return sendComposerMessage(AUTO_CONTINUE_TEXT);
  }}

  function bridgeRequest(method,url,data=null) {{
    return new Promise((resolve,reject)=>{{
      GM_xmlhttpRequest({{
        method,
        url,
        headers:{{
          "Content-Type":"application/json",
          "X-AI-Beast-Bridge":TOKEN
        }},
        data:data===null ? undefined : JSON.stringify(data),
        timeout:10000,
        onload:r=>{{
          if (r.status<200 || r.status>=300) {{
            reject(new Error("HTTP "+r.status));
            return;
          }}
          try {{
            resolve(JSON.parse(r.responseText || "{{}}"));
          }} catch (e) {{
            reject(e);
          }}
        }},
        onerror:()=>reject(new Error("connection error")),
        ontimeout:()=>reject(new Error("timeout")),
      }});
    }});
  }}

  let wakePollBusy=false;

  async function pollPendingWake() {{
    if (wakePollBusy) return;
    if (!window.location.pathname.startsWith(PROJECT_PATH)) return;
    wakePollBusy=true;
    try {{
      const reply=await bridgeRequest("GET",WAKE_NEXT_ENDPOINT);
      const wake=reply && reply.wake;
      if (!wake || !wake.id || !wake.message) return;

      const sentKey="ai-beast-wake-sent:"+wake.id;
      if (localStorage.getItem(sentKey)!=="done") {{
        if (isGenerating()) return;
        const composer=composerNode();
        if (!composer || composerText(composer)) return;
        const sent=await sendComposerMessage(wake.message);
        if (!sent.ok) {{
          status("wake waiting: "+sent.reason,false);
          return;
        }}
        localStorage.setItem(sentKey,"done");
        status("wake sent "+wake.id);
      }}

      await bridgeRequest("POST",WAKE_ACK_ENDPOINT,{{id:wake.id}});
      localStorage.removeItem(sentKey);
    }} catch (e) {{
      // Quiet by design: queue remains durable and scan() keeps normal status.
    }} finally {{
      wakePollBusy=false;
    }}
  }}
''',
"generic composer send and wake poller"
)

once(
'''  status("active");
  scan();
  new MutationObserver(()=>scan()).observe(document.body,{{subtree:true,childList:true,characterData:true}});
  setInterval(scan,2000);
''',
'''  status("active");
  scan();
  pollPendingWake();
  new MutationObserver(()=>scan()).observe(document.body,{{subtree:true,childList:true,characterData:true}});
  setInterval(scan,2000);
  setInterval(pollPendingWake,2000);
''',
"poll startup"
)

once(
'''        if path=="/bridge.user.js":
            return self.reply(200,user_script(),"text/javascript; charset=utf-8")
        return self.reply(404,{"error":"not-found"})
''',
'''        if path=="/bridge.user.js":
            return self.reply(200,user_script(),"text/javascript; charset=utf-8")
        if path=="/wake-next":
            if self.headers.get("X-AI-Beast-Bridge","")!=CFG["token"]:
                return self.reply(403,{"error":"bad-token"})
            origin=self.headers.get("Origin","")
            if origin and origin not in ALLOWED_ORIGINS:
                return self.reply(403,{"error":"bad-origin"})
            with LOCK:
                files=sorted(WAKE_QUEUE.glob("*.json"))
                wake=None
                while files:
                    candidate=files.pop(0)
                    try:
                        obj=json.loads(candidate.read_text())
                        if (
                            isinstance(obj,dict)
                            and obj.get("id")==candidate.stem
                            and isinstance(obj.get("message"),str)
                            and obj.get("message")
                        ):
                            wake=obj
                            break
                    except Exception:
                        pass
                    candidate.unlink(missing_ok=True)
            return self.reply(200,{"wake":wake})
        return self.reply(404,{"error":"not-found"})
''',
"GET wake-next"
)

once(
'''    def do_POST(self):
        if urlparse(self.path).path!="/job":
            return self.reply(404,{"error":"not-found"})

        if self.headers.get("X-AI-Beast-Bridge","")!=CFG["token"]:
''',
'''    def do_POST(self):
        path=urlparse(self.path).path
        if path=="/wake-ack":
            if self.headers.get("X-AI-Beast-Bridge","")!=CFG["token"]:
                return self.reply(403,{"error":"bad-token"})
            origin=self.headers.get("Origin","")
            if origin and origin not in ALLOWED_ORIGINS:
                return self.reply(403,{"error":"bad-origin"})
            try:
                n=int(self.headers.get("Content-Length","0"))
            except ValueError:
                return self.reply(400,{"error":"bad-length"})
            if n<2 or n>10000:
                return self.reply(413,{"error":"body-size"})
            try:
                payload=json.loads(self.rfile.read(n))
            except Exception:
                return self.reply(400,{"error":"bad-json"})
            wake_id=str(payload.get("id") or "")
            if not re.fullmatch(r"[0-9]{16,20}-[0-9a-f]{16}",wake_id):
                return self.reply(400,{"error":"bad-wake-id"})
            with LOCK:
                target=WAKE_QUEUE/f"{wake_id}.json"
                existed=target.exists()
                target.unlink(missing_ok=True)
            return self.reply(200,{"status":"acked","id":wake_id,"existed":existed})

        if path!="/job":
            return self.reply(404,{"error":"not-found"})

        if self.headers.get("X-AI-Beast-Bridge","")!=CFG["token"]:
''',
"POST wake-ack"
)

p.write_text(s)
PY

python3 -m py_compile "$INGRESS"

echo "=== PATCH CHECK ==="
grep -n -E 'BRIDGE_VERSION|WAKE_NEXT_ENDPOINT|WAKE_ACK_ENDPOINT|wake-next|wake-ack|pollPendingWake|sendComposerMessage' "$INGRESS" | head -80

echo
echo "=== RESTART CLEANLY UNDER SYSTEMD ==="
systemctl --user stop "$SERVICE" || true
pkill -f '^/usr/bin/python3 /home/bking/AI/chatgpt-job-ingress/ingress.py$' || true
sleep 1
systemctl --user start "$SERVICE"
sleep 1
systemctl --user is-active "$SERVICE"
systemctl --user status "$SERVICE" --no-pager | head -25

echo
echo "=== HEALTH ==="
curl -fsS http://127.0.0.1:8767/health
echo
curl -fsS http://127.0.0.1:8767/bridge.user.js | grep -E 'BRIDGE_VERSION = "2.11"|WAKE_NEXT_ENDPOINT' | head -10
