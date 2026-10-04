#!/usr/bin/env python3
import unittest
import leaf_contract


DONE=(
    "A documented command starts a self-contained server on 127.0.0.1 "
    "and returns an HTML page with HTTP 200."
)
GOOD=(
    "PORT=8787 node -e 'const{spawn}=require(\"child_process\");"
    "const h=spawn(\"node\",[\"server.js\"],{env:{...process.env,PORT:\"8787\"},stdio:\"ignore\"});"
    "setTimeout(async()=>{try{const r=await fetch(\"http://127.0.0.1:8787/\");"
    "const b=await r.text();if(r.status!==200)throw new Error(\"status \"+r.status);"
    "if(!/<canvas/i.test(b))throw new Error(\"no canvas\");"
    "h.kill();process.exit(0)}catch(e){h.kill();process.exit(1)}},2500)'"
)


class NodeEmbeddedServiceVerifyTests(unittest.TestCase):
    def test_r4_corrected_node_spawn_fetch_verify_is_runtime_service_evidence(self):
        self.assertTrue(leaf_contract.embedded_node_server_http_verify(GOOD))
        self.assertEqual(leaf_contract.validate_verify_command(GOOD),[])
        self.assertEqual(leaf_contract.validate_verify_adequacy(DONE,GOOD),[])

    def test_invalid_r4_spawn_syntax_is_not_service_evidence(self):
        bad=GOOD.replace('spawn(\"node\",[\"server.js\"]','spawn(\"node\":[\"server.js\"]')
        self.assertFalse(leaf_contract.embedded_node_server_http_verify(bad))
        errors=leaf_contract.validate_verify_command(bad)
        self.assertTrue(any("invalid JavaScript syntax" in e for e in errors))

    def test_static_string_mentions_do_not_count(self):
        bad=(
            "node -e 'console.log(\"spawn(node,[server.js]) fetch(http://localhost) "
            "throw new Error\")'"
        )
        self.assertFalse(leaf_contract.embedded_node_server_http_verify(bad))
        self.assertTrue(leaf_contract.validate_verify_adequacy(DONE,bad))

    def test_launch_without_http_probe_does_not_count(self):
        bad=(
            "node -e 'const{spawn}=require(\"child_process\");"
            "const h=spawn(\"node\",[\"server.js\"]);"
            "setTimeout(()=>{h.kill();process.exit(1)},100)'"
        )
        self.assertFalse(leaf_contract.embedded_node_server_http_verify(bad))
        self.assertTrue(leaf_contract.validate_verify_adequacy(DONE,bad))

    def test_http_probe_without_server_launch_does_not_count(self):
        bad=(
            "node -e 'fetch(\"http://localhost:8787/\").then(r=>{"
            "if(r.status!==200)throw new Error(\"bad\")})'"
        )
        self.assertFalse(leaf_contract.embedded_node_server_http_verify(bad))
        self.assertTrue(leaf_contract.validate_verify_adequacy(DONE,bad))

    def test_r6_status_code_ternary_exit_is_fail_closed_service_evidence(self):
        command=(
            "node -e \"const{spawn}=require('child_process');"
            "const p=spawn('node',['server.js'],{stdio:['ignore','pipe','pipe']});"
            "let code=0;const t0=Date.now();(async()=>{"
            "while(Date.now()-t0<8000){try{const r=await fetch("
            "'http://127.0.0.1:8000/');code=r.status;if(code)break}catch(e){}"
            "await new Promise(s=>setTimeout(s,150))}"
            "p.kill();process.exit(code===200?0:1)})()\""
        )
        self.assertTrue(leaf_contract.embedded_node_server_http_verify(command))
        self.assertEqual(leaf_contract.validate_verify_command(command),[])
        self.assertEqual(leaf_contract.validate_verify_adequacy(DONE,command),[])

    def test_unrelated_constant_ternary_exit_does_not_count(self):
        bad=(
            "node -e 'const{spawn}=require(\"child_process\");"
            "const p=spawn(\"node\",[\"server.js\"]);"
            "fetch(\"http://localhost:8787/\").then(()=>{"
            "p.kill();process.exit(true?0:1)})'"
        )
        self.assertFalse(leaf_contract.embedded_node_server_http_verify(bad))
        self.assertTrue(leaf_contract.validate_verify_adequacy(DONE,bad))


if __name__=="__main__":
    unittest.main()
