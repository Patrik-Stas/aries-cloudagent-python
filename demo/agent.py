import asyncio
import functools
import json
import logging
import os
import random
import subprocess

from aiohttp import web, ClientSession, ClientRequest, ClientError

from .utils import flatten, log_json, log_msg, log_timer, output_reader

LOGGER = logging.getLogger(__name__)

DEFAULT_POSTGRES = bool(os.getenv("POSTGRES"))
DEFAULT_INTERNAL_HOST = "127.0.0.1"
DEFAULT_EXTERNAL_HOST = "localhost"
DEFAULT_BIN_PATH = "../bin"
DEFAULT_PYTHON_PATH = ".."

RUN_MODE = os.getenv("RUNMODE")

if RUN_MODE == "docker":
    DEFAULT_INTERNAL_HOST = os.getenv("DOCKERHOST") or "host.docker.internal"
    DEFAULT_EXTERNAL_HOST = DEFAULT_INTERNAL_HOST
    DEFAULT_BIN_PATH = "./bin"
    DEFAULT_PYTHON_PATH = "."


async def default_genesis_txns():
    genesis = None
    try:
        if RUN_MODE == "docker":
            async with ClientSession() as session:
                async with session.get(
                    f"http://{DEFAULT_EXTERNAL_HOST}:9000/genesis"
                ) as resp:
                    genesis = await resp.text()
        else:
            with open("local-genesis.txt", "r") as genesis_file:
                genesis = genesis_file.read()
    except Exception:
        LOGGER.exception("Error loading genesis transactions:")
    return genesis


class DemoAgent:
    def __init__(
        self,
        ident: str,
        http_port: int,
        admin_port: int,
        internal_host: str = None,
        external_host: str = None,
        genesis_data: str = None,
        label: str = None,
        color: str = None,
        prefix: str = None,
        timing: bool = False,
        postgres: bool = None,
        extra_args=None,
        **params,
    ):
        self.ident = ident
        self.http_port = http_port
        self.admin_port = admin_port
        self.internal_host = internal_host or DEFAULT_INTERNAL_HOST
        self.external_host = external_host or DEFAULT_EXTERNAL_HOST
        self.genesis_data = genesis_data
        self.label = label or ident
        self.color = color
        self.prefix = prefix
        self.timing = timing
        self.postgres = DEFAULT_POSTGRES if postgres is None else postgres
        self.extra_args = extra_args

        self.endpoint = f"http://{self.external_host}:{http_port}"
        self.admin_url = f"http://{self.external_host}:{admin_port}"
        self.webhook_port = None
        self.webhook_url = None
        self.webhook_site = None
        self.params = params
        self.proc = None
        self.client_session: ClientSession = ClientSession()

        rand_name = str(random.randint(100_000, 999_999))
        self.seed = (
            params.get("seed") or ("my_seed_000000000000000000000000" + rand_name)[-32:]
        )
        self.storage_type = params.get("storage_type")
        self.wallet_type = params.get("wallet_type", "indy")
        self.wallet_name = params.get("wallet_name") or \
            self.ident.lower().replace(' ', '') + rand_name
        self.wallet_key = params.get("wallet_key") or self.ident + rand_name
        self.did = None

    def get_agent_args(self):
        result = [
            ("--endpoint", self.endpoint),
            ("--label", self.label),
            "--auto-respond-messages",
            "--accept-invites",
            "--accept-requests",
            "--auto-ping-connection",
            ("--inbound-transport", "http", "0.0.0.0", str(self.http_port)),
            ("--outbound-transport", "http"),
            ("--admin", "0.0.0.0", str(self.admin_port)),
            ("--wallet-type", self.wallet_type),
            ("--wallet-name", self.wallet_name),
            ("--wallet-key", self.wallet_key),
            ("--seed", self.seed),
        ]
        if self.genesis_data:
            result.append(("--genesis-transactions", self.genesis_data))
        if self.storage_type:
            result.append(("--storage-type", self.storage_type))
        if self.timing:
            result.append("--timing")
        if self.postgres:
            result.extend(
                [
                    ("--wallet-storage-type", "postgres_storage"),
                    (
                        "--wallet-storage-config",
                        json.dumps(
                            {
                                "url": f"{self.internal_host}:5432",
                                "tls": "None",
                                "max_connections": 5,
                                "min_idle_time": 0,
                                "connection_timeout": 10,
                            }
                        ),
                    ),
                    (
                        "--wallet-storage-creds",
                        json.dumps(
                            {
                                "account": "postgres",
                                "password": "mysecretpassword",
                                "admin_account": "postgres",
                                "admin_password": "mysecretpassword",
                            }
                        ),
                    ),
                ]
            )
        if self.webhook_url:
            result.append(("--webhook-url", self.webhook_url))
        if self.extra_args:
            result.extend(self.extra_args)

        return result

    @property
    def prefix_str(self):
        if self.prefix:
            return f"{self.prefix:10s} |"

    async def register_did(self, ledger_url: str = None, alias: str = None):
        self.log(f"Registering {self.ident} with seed {self.seed}")
        if not ledger_url:
            ledger_url = f"http://{self.external_host}:9000"
        data = {"alias": alias or self.ident, "seed": self.seed, "role": "TRUST_ANCHOR"}
        async with self.client_session.post(
            ledger_url + "/register", json=data
        ) as resp:
            if resp.status != 200:
                raise Exception(f"Error registering DID, response code {resp.status}")
            nym_info = await resp.json()
            self.did = nym_info["did"]
        self.log(f"Got DID: {self.did}")

    def handle_output(self, *output, source: str = None, **kwargs):
        end = "" if source else "\n"
        if source == "stderr":
            color = "fg:ansired"
        elif not source:
            color = self.color or "fg:ansiblue"
        else:
            color = None
        log_msg(*output, color=color, prefix=self.prefix_str, end=end, **kwargs)

    def log(self, *msg, **kwargs):
        self.handle_output(*msg, **kwargs)

    def log_json(self, data, label: str = None, **kwargs):
        log_json(data, label=label, prefix=self.prefix_str, **kwargs)

    def log_timer(self, label: str, show: bool = True, **kwargs):
        return log_timer(label, show, logger=self.log, **kwargs)

    def _process(self, args, env, loop):
        proc = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            encoding="utf-8",
        )
        loop.run_in_executor(
            None,
            output_reader,
            proc.stdout,
            functools.partial(self.handle_output, source="stdout"),
        )
        loop.run_in_executor(
            None,
            output_reader,
            proc.stderr,
            functools.partial(self.handle_output, source="stderr"),
        )
        return proc

    def get_process_args(self, bin_path: str = None):
        cmd_path = "acagent"
        if bin_path is None:
            bin_path = DEFAULT_BIN_PATH
        if bin_path:
            cmd_path = os.path.join(bin_path, cmd_path)
        return list(flatten((["python3", cmd_path], self.get_agent_args())))

    async def start_process(
        self, python_path: str = None, bin_path: str = None, wait: bool = True
    ):
        my_env = os.environ.copy()
        python_path = DEFAULT_PYTHON_PATH if python_path is None else python_path
        if python_path:
            my_env["PYTHONPATH"] = python_path

        agent_args = self.get_process_args(bin_path)

        # start agent sub-process
        loop = asyncio.get_event_loop()
        self.proc = await loop.run_in_executor(
            None, self._process, agent_args, my_env, loop
        )
        if wait:
            await self.detect_process()

    def _terminate(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=0.5)
                self.log(f"Exited with return code {self.proc.returncode}")
            except subprocess.TimeoutExpired:
                msg = "Process did not terminate in time"
                self.log(msg)
                raise Exception(msg)

    async def terminate(self):
        loop = asyncio.get_event_loop()
        if self.proc:
            await loop.run_in_executor(None, self._terminate)
        await self.client_session.close()
        if self.webhook_site:
            await self.webhook_site.stop()

    async def listen_webhooks(self, webhook_port):
        self.webhook_port = webhook_port
        self.webhook_url = f"http://{self.external_host}:{str(webhook_port)}/webhooks"
        app = web.Application()
        app.add_routes([web.post("/webhooks/topic/{topic}/", self._receive_webhook)])
        runner = web.AppRunner(app)
        await runner.setup()
        self.webhook_site = web.TCPSite(runner, "0.0.0.0", webhook_port)
        await self.webhook_site.start()

    async def _receive_webhook(self, request: ClientRequest):
        topic = request.match_info["topic"]
        payload = await request.json()
        await self.handle_webhook(topic, payload)
        return web.Response(text="")

    async def handle_webhook(self, topic: str, payload):
        if topic != "webhook":  # would recurse
            method = getattr(self, f"handle_{topic}", None)
            if method:
                await method(payload)

    async def admin_request(self, method, path, data=None, text=False):
        async with self.client_session.request(
            method, self.admin_url + path, json=data
        ) as resp:
            if resp.status < 200 or resp.status > 299:
                raise Exception(f"Unexpected HTTP response: {resp.status}")
            resp_text = await resp.text()
            if not resp_text and not text:
                return None
            if not text:
                try:
                    return json.loads(resp_text)
                except json.JSONDecodeError as e:
                    raise Exception(f"Error decoding JSON: {resp_text}") from e
            return resp_text

    async def admin_GET(self, path, text=False):
        return await self.admin_request("GET", path, None, text)

    async def admin_POST(self, path, data=None, text=False):
        return await self.admin_request("POST", path, data, text)

    async def detect_process(self):
        text = None
        for i in range(10):
            # wait for process to start and retrieve swagger content
            await asyncio.sleep(2.0)
            try:
                async with self.client_session.get(
                    self.admin_url + "/api/docs/swagger.json"
                ) as resp:
                    if resp.status == 200:
                        text = await resp.text()
                        break
            except ClientError:
                text = None
                continue
        if not text:
            raise Exception(f"Timed out waiting for agent process to start")
        if "Aries Cloud Agent" not in text:
            raise Exception(f"Unexpected response from agent process")

    async def fetch_timing(self):
        status = await self.admin_GET("/status")
        return status.get("timing")

    def format_timing(self, timing: dict) -> dict:
        result = []
        for name, count in timing["count"].items():
            result.append(
                (
                    name[:35],
                    count,
                    timing["total"][name],
                    timing["avg"][name],
                    timing["min"][name],
                    timing["max"][name],
                )
            )
        result.sort(key=lambda row: row[2], reverse=True)
        yield "{:35} | {:>12} {:>12} {:>10} {:>10} {:>10}".format(
            "", "count", "total", "avg", "min", "max"
        )
        yield "=" * 96
        yield from (
            "{:35} | {:12d} {:12.3f} {:10.3f} {:10.3f} {:10.3f}".format(*row)
            for row in result
        )
        yield ""

    async def reset_timing(self):
        await self.admin_POST("/status/reset", text=True)
