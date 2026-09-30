import os
from pathlib import Path
import json
import time
import hmac
import hashlib
import random
import base64
import datetime
import urllib.parse
import asyncio
import aiohttp
import msgpack
from Crypto.Cipher import AES
from Crypto.PublicKey import RSA
from Crypto.Cipher import PKCS1_OAEP
from Crypto.Hash import SHA256
from Crypto.Cipher import PKCS1_v1_5
import re
import secrets
import html
from bs4 import BeautifulSoup

from dataclasses import dataclass, field
from typing import Optional

BASE_DIR = Path(__file__).resolve().parent
RESULT_FILE = BASE_DIR / "result.json"
TSUM_DEBUG = os.getenv("TSUM_DEBUG", "0") == "1"
TSUM_GACHA_DEBUG = os.getenv("TSUM_GACHA_DEBUG", "1") == "1"
GACHA_DIAGNOSTIC_FORCE = True
TSUM_GACHA_MAX_CALLS = max(1, int(os.getenv("TSUM_GACHA_MAX_CALLS", "500") or "500"))
TSUM_APP_VER = os.getenv("TSUM_APP_VER", "12.10.1").strip() or "12.10.1"
TSUM_RES_VER = os.getenv("TSUM_RES_VER", "12.10.1").strip() or "12.10.1"

@dataclass
class TsumInfo:
    tsumid: int; lv: int; exp: int; skilllv: int; vanishcnt: int
    tsumcnt: int; lbcoin: int; limitlv: int; nextlimitlv: int
    nextlbcoin: int; skinbit: int; setflg: int; fav: str
    regdt: int; discountflg: int; wappen: Optional[list[int]] = None

    @property
    def is_mytsum(self) -> bool: return self.setflg == 1
    @property
    def has_skin(self) -> bool: return self.skinbit != 0

    @classmethod
    def from_dict(cls, d: dict) -> "TsumInfo":
        return cls(
            tsumid=d["tsumid"], lv=d["lv"], exp=d["exp"],
            skilllv=d["skilllv"], vanishcnt=d["vanishcnt"],
            tsumcnt=d["tsumcnt"], lbcoin=d["lbcoin"],
            limitlv=d["limitlv"], nextlimitlv=d["nextlimitlv"],
            nextlbcoin=d["nextlbcoin"] or 0,
            skinbit=d["skinbit"], setflg=d["setflg"],
            fav=d["fav"], regdt=int(d["regdt"]),
            discountflg=d["discountflg"], wappen=d.get("wappen"),
        )

@dataclass
class GachaInfo:
    gachaid: int; multiflg: int; compflg: int

    @property
    def can_multi(self) -> bool: return self.multiflg == 1
    @property
    def is_completed(self) -> bool: return self.compflg == 1

    @classmethod
    def from_dict(cls, d: dict) -> "GachaInfo":
        return cls(gachaid=d["gachaid"], multiflg=d["multiflg"], compflg=d["compflg"])

@dataclass
class UserInfo:
    name: str; lv: int; score: int; bcoin: int; bmedal: int
    bruby: int; bheart: int; maxcoin: int; maxcombo: int
    maxchain: int; playcnt: int; range: str; expper: int; regdt: int
    pcoin: int = 0; pmedal: int = 0; pruby: int = 0; pheart: int = 0

    @property
    def coin_total(self) -> int:
        return max(0, self.bcoin + self.pcoin)

    @property
    def medal_total(self) -> int:
        return max(0, self.bmedal + self.pmedal)

    @property
    def ruby_total(self) -> int:
        return max(0, self.bruby + self.pruby)

    @classmethod
    def from_dict(cls, d: dict) -> "UserInfo":
        return cls(
            name=d["name"], lv=d["lv"], score=d["score"],
            bcoin=d.get("bcoin", 0), bmedal=d.get("bmedal", 0), bruby=d.get("bruby", 0),
            bheart=d.get("bheart", 0), maxcoin=d.get("maxcoin", 0),
            maxcombo=d.get("maxcombo", 0), maxchain=d.get("maxchain", 0),
            playcnt=d.get("playcnt", 0), range=d.get("range", ""),
            expper=d.get("expper", 0), regdt=int(d.get("regdt", 0)),
            pcoin=d.get("pcoin", 0), pmedal=d.get("pmedal", 0),
            pruby=d.get("pruby", 0), pheart=d.get("pheart", 0),
        )

@dataclass
class StatEntry:
    type: int; value: int; tsumid: int

@dataclass
class TsumScore:
    tsumid: int; score: int

@dataclass
class MyDataInfo:
    totalexp: int; totalplaycnt: int; totalskillcnt: int
    logincnt: int; currentlogincnt: int; maxlogincnt: int
    hometsum: int; homebgid: int
    statslist: list[StatEntry] = field(default_factory=list)
    tsumscorelist: list[TsumScore] = field(default_factory=list)

    @property
    def mytsum_id(self) -> int:
        for s in self.statslist:
            if s.type == 9: return s.tsumid
        return 0

    @classmethod
    def from_dict(cls, d: dict) -> "MyDataInfo":
        return cls(
            totalexp=d["totalexp"], totalplaycnt=d["totalplaycnt"],
            totalskillcnt=d["totalskillcnt"], logincnt=d["logincnt"],
            currentlogincnt=d["currentlogincnt"], maxlogincnt=d["maxlogincnt"],
            hometsum=d["hometsum"], homebgid=d["homebgid"],
            statslist=[StatEntry(s["type"], s["value"], s["tsumid"]) for s in d.get("statslist", [])],
            tsumscorelist=[TsumScore(t["tsum"], t["score"]) for t in d.get("tsumscorelist", [])],
        )

@dataclass
class ItemInfo:
    id: int; type: int; cnt: int

    @classmethod
    def from_dict(cls, d: dict) -> "ItemInfo":
        return cls(id=d["id"], type=d["type"], cnt=d["cnt"])

@dataclass
class GetInfoResponse:
    userinfo: UserInfo; mydatainfo: MyDataInfo
    tsuminfo: list[TsumInfo] = field(default_factory=list)
    iteminfo: list[ItemInfo] = field(default_factory=list)
    gachainfo: list[GachaInfo] = field(default_factory=list)
    retcode: int = 0; retmsg: str = ""; retsubcode: int = 0

    @property
    def mytsum(self) -> Optional[TsumInfo]:
       
        t = next((t for t in self.tsuminfo if t.is_mytsum), None)
        if t:
            return t

        t = self.get_tsum(self.mydatainfo.hometsum)
        if t:
            return t

        return self.get_tsum(1)

    def get_tsum(self, tsumid: int) -> Optional[TsumInfo]:
        return next((t for t in self.tsuminfo if t.tsumid == tsumid), None)

    def get_item(self, item_id: int, item_type: int) -> int:
        return next((i.cnt for i in self.iteminfo if i.id == item_id and i.type == item_type), 0)

    @property
    def available_gachas(self) -> list[GachaInfo]:
        return [g for g in self.gachainfo]

    @property
    def play_items(self) -> dict[str, int]:
        NAME = {1: "5to4", 2: "4to3", 3: "time+5", 4: "score+2", 5: "coin+2", 7: "other"}
        return {NAME.get(i.id, str(i.id)): i.cnt for i in self.iteminfo if i.type == 4}

    @classmethod
    def from_dict(cls, d: dict) -> "GetInfoResponse":
        return cls(
            userinfo=UserInfo.from_dict(d["userinfo"]),
            mydatainfo=MyDataInfo.from_dict(d["mydatainfo"]),
            tsuminfo=[TsumInfo.from_dict(t) for t in d.get("tsuminfo", [])],
            iteminfo=[ItemInfo.from_dict(i) for i in d.get("iteminfo", [])],
            gachainfo=[GachaInfo.from_dict(g) for g in d.get("gachainfo", [])],
            retcode=d.get("retcode", 0),
            retmsg=d.get("retmsg", ""),
            retsubcode=d.get("retsubcode", 0),
        )

class SecureSessionCrypto:
    @staticmethod
    def generate_secure_random(length: int) -> bytes:
        return os.urandom(length)

    @staticmethod
    def rsa_encrypt_oaep(data: bytes, public_key_b64: str) -> str:
        pub_key_der = base64.b64decode(public_key_b64)
        pub_key     = RSA.import_key(pub_key_der)
        cipher      = PKCS1_OAEP.new(pub_key, hashAlgo=SHA256)
        return base64.b64encode(cipher.encrypt(data)).decode()

    @staticmethod
    def aes_gcm_encrypt(plaintext_str: str, key: bytes) -> bytes:
        iv              = SecureSessionCrypto.generate_secure_random(12)
        cipher          = AES.new(key, AES.MODE_GCM, nonce=iv, mac_len=16)
        ciphertext, tag = cipher.encrypt_and_digest(plaintext_str.encode("utf-8"))
        return iv + ciphertext + tag

    @staticmethod
    def aes_gcm_decrypt(data: bytes, key: bytes) -> str:
        iv         = data[:12]
        ciphertext = data[12:-16]
        tag        = data[-16:]
        cipher     = AES.new(key, AES.MODE_GCM, nonce=iv, mac_len=16)
        return cipher.decrypt_and_verify(ciphertext, tag).decode("utf-8")

class LineOAuthLogin:
    BASE_URL     = "https://access.line.me"
    TOKEN_URL    = "https://api.line.me/oauth2/v2.1/token"
    CLIENT_ID    = "1381161556"
    REDIRECT_URI = "intent://result#Intent;package=com.linecorp.LGTMTM;scheme=lineauth;end"
    UA_BROWSER   = "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Mobile Safari/537.36"
    UA_APP       = f"com.linecorp.LGTMTM/{TSUM_APP_VER} ChannelSDK/5.11.1 (Linux; U; Android 11; ja-JP; Pixel 4a (5G) Build/RQ3A.211001.001)"

    def __init__(self, line_id: str, password: str,captcha_callback=None):
        self.line_id  = line_id
        self.password = password
        self.captcha_callback = captcha_callback

    @staticmethod
    def _pkce() -> tuple[str, str]:
        verifier  = secrets.token_urlsafe(64)[:87]
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode()).digest()
        ).rstrip(b"=").decode()
        return verifier, challenge

    @staticmethod
    def _encrypt_password(line_id: str, password: str, rsa_key_str: str, session_key: str) -> tuple[str, str, str]:
        def build_message(sk, lid, pw) -> bytes:
            def lc(s): return bytes([len(s)])
            return lc(sk) + sk.encode() + lc(lid) + lid.encode() + lc(pw) + pw.encode()

        key_id, n_hex, e_hex = rsa_key_str.split(",")
        message = build_message(session_key, line_id, password)
        n, e    = int(n_hex, 16), int(e_hex, 16)
        pub_key = RSA.construct((n, e))
        cipher  = PKCS1_v1_5.new(pub_key)
        encrypted = cipher.encrypt(message)
        user_id   = hashlib.md5(line_id.encode()).hexdigest()
        return key_id, encrypted.hex(), user_id

    @staticmethod
    def detect_captcha(html_text: str) -> dict:
        soup    = BeautifulSoup(html_text, "html.parser")
        app_tag = soup.find("app")
        if not app_tag:
            return {"has_captcha": False}
        raw  = app_tag.get("app-data", "{}")
        data = json.loads(raw).get("props", {})
        return {
            "has_captcha":   data.get("is-captcha", False),
            "is_recaptcha":  data.get("is-recaptcha", False),
            "captcha_url":   data.get("captcha-url"),
            "recaptcha_key": data.get("recaptcha-key"),
        }

    async def login(self) -> tuple[str, str]:
        state            = secrets.token_urlsafe(12)
        verifier, challenge = self._pkce()
        redirect_encoded = urllib.parse.quote(self.REDIRECT_URI, safe="")
        consent_params   = (
            f"response_type=code&client_id={self.CLIENT_ID}&state={state}"
            f"&code_challenge={challenge}&code_challenge_method=S256"
            f"&redirect_uri={redirect_encoded}&sdk_ver=5.11.1"
            f"&scope=friends%20message.write%20profile%20trident.user.key&bot_prompt=normal"
        )
        return_uri         = "/oauth2/v2.1/authorize/consent?" + consent_params
        return_uri_encoded = urllib.parse.quote(return_uri, safe="")
        login_url = (
            f"{self.BASE_URL}/oauth2/v2.1/login"
            f"?returnUri={return_uri_encoded}&loginChannelId={self.CLIENT_ID}&ui_locales=ja"
        )

        initial_cookies = {
            "_trmccid": "b65a0383064e5ba3",
            "_trmcuser": '{"id":""}',
            "_ldbrbid": "lo__AnPxO-v2bp6NebhD5byaRvdzIi6p2qEKN4m8BcCumgg",
            "LC": "c63d89bb355870d5cf9d12cec48ddae1b557d5843de8747523cb72aa585c2d8f",
        }

        browser_headers = {
            "Accept-Language":           "ja-JP",
            "Upgrade-Insecure-Requests": "1",
            "User-Agent":                self.UA_BROWSER,
            "Accept":                    "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        }

        async with aiohttp.ClientSession(cookies=initial_cookies) as session:

            print("[OAuth] Step 1: session init")
            async with session.get(login_url, headers=browser_headers) as resp:
                html_text = await resp.text()

            captcha_info = self.detect_captcha(html_text)
            captcha      = None
            if captcha_info["has_captcha"]:
                print(f"[OAuth] キャプチャあり: {captcha_info['captcha_url']}")
                async with session.get(
                    self.BASE_URL + captcha_info["captcha_url"],
                    headers={"User-Agent": self.UA_BROWSER, "Referer": login_url},
                ) as img_resp:
                    img_data = await img_resp.read()

                if self.captcha_callback:
                    captcha = await self.captcha_callback(img_data)
                else:
                    with open("captcha.png", "wb") as f:
                        f.write(img_data)
                    captcha = input("CaptchaCode: ")

            m = re.search(r'app-data="(.+?)"', html_text)
            if not m:
                raise Exception("app-data not found")
            data        = json.loads(html.unescape(m.group(1)))
            captcha_url = data["props"]["captcha-url"]
            captcha_key = next(
                (f["value"] for f in data["slots"]["serverside_field"] if f.get("name") == "captchaKey"),
                None,
            )

            ts = int(time.time() * 1000)
            async with session.get(
                f"{self.BASE_URL}/oauth2/v2.1/authn/session?_={ts}",
                headers={"User-Agent": self.UA_BROWSER, "Accept": "application/json", "Referer": login_url},
            ) as resp:
                session_id = (await resp.json())["result"]
            print(f"[OAuth] sessionId: {session_id[:20]}...")

            ts = int(time.time() * 1000)
            async with session.get(
                f"{self.BASE_URL}/oauth2/v2.1/authn/keys/line?sessionId={session_id}&_={ts}",
                headers={"User-Agent": self.UA_BROWSER, "X-Requested-With": "XMLHttpRequest", "Referer": login_url},
            ) as resp:
                key_data    = await resp.json()
            session_key = key_data["session_key"]
            rsa_key     = key_data["rsa_key"]

            key_id, encrypted_pw, user_id = self._encrypt_password(self.line_id, self.password, rsa_key, session_key)
            cookies = {c.key: c.value for c in session.cookie_jar}
            csrf_token = cookies.get("X-SCGW-CSRF-Token", "")

            print("[OAuth] Step 4: authenticate")
            post_data = {
                "userId": user_id, "encUserId": "", "id": key_id,
                "password": encrypted_pw, "idProvider": "1",
                "sessionId": session_id, "encKeyId": key_id,
                "loginChannelId": self.CLIENT_ID, "returnUri": return_uri,
                "displayType": "M", "captchaKey": captcha_key,
                "__csrf": csrf_token, "lang": "ja",
                **( {"captcha": captcha} if captcha is not None else {} ),
            }
            async with session.post(
                f"{self.BASE_URL}/oauth2/v2.1/authenticate",
                data=post_data,
                headers={
                    "User-Agent": self.UA_BROWSER, "Origin": self.BASE_URL,
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Referer": login_url, "Upgrade-Insecure-Requests": "1",
                },
                allow_redirects=True,
            ) as resp:
                last_url = str(resp.url)

            print("[OAuth] Step 5: consent")
            async with session.post(
                f"{self.BASE_URL}/oauth2/v2.1/authorize/consent",
                data=[
                    ("addFriend", "true"), ("allPermission", "P"), ("approvedPermission", "P"),
                    ("allPermission", "F"), ("approvedPermission", "F"),
                    ("allPermission", "M"), ("approvedPermission", "M"),
                    ("allPermission", "TUK"), ("approvedPermission", "TUK"),
                    ("channelId", self.CLIENT_ID), ("addFriendMode", "ALREADY_FRIENDED_MODE"),
                    ("__csrf", csrf_token), ("lang", "ja"), ("allow", "true"),
                ],
                headers={
                    "User-Agent": self.UA_BROWSER, "Origin": self.BASE_URL,
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Referer": last_url, "Upgrade-Insecure-Requests": "1",
                },
                allow_redirects=False,
            ) as resp:
                location = resp.headers.get("Location", "")

            code = urllib.parse.parse_qs(urllib.parse.urlparse(location).query).get("code", [None])[0]
            if not code:
                raise ValueError("ログインに失敗しました。メアパスが間違っています。")
            print(f"[OAuth] code: {code}")

            print("[OAuth] Step 6: token")
            async with aiohttp.ClientSession() as token_session:
                async with token_session.post(
                    self.TOKEN_URL,
                    data={
                        "grant_type": "authorization_code", "code": code,
                        "redirect_uri": self.REDIRECT_URI, "client_id": self.CLIENT_ID,
                        "code_verifier": verifier, "id_token_key_type": "JWK",
                        "client_version": "LINE SDK Android v5.11.1",
                    },
                    headers={
                        "User-Agent": self.UA_APP,
                        "Content-Type": "application/x-www-form-urlencoded",
                    },
                ) as resp:
                    token_data = await resp.json()

        print("[OAuth] token取得 OK")
        return token_data["access_token"], token_data["refresh_token"]

class LineTokenManager:
    TOKEN_URL  = "https://api.line.me/oauth2/v2.1/token"
    CLIENT_ID  = "1381161556"
    USER_AGENT = f"com.linecorp.LGTMTM/{TSUM_APP_VER} ChannelSDK/5.11.1 (Linux; U; Android 11; ja-JP; Pixel 4a (5G) Build/RQ3A.211001.001)"

    def __init__(self, refresh_token: str):
        self.refresh_token = refresh_token
        self.access_token  = None
        self.expires_at    = 0

    async def refresh(self) -> str:
        print("[LINE] access_token 更新中...")
        async with aiohttp.ClientSession() as session:
            async with session.post(
                self.TOKEN_URL,
                data={
                    "grant_type":    "refresh_token",
                    "refresh_token": self.refresh_token,
                    "client_id":     self.CLIENT_ID,
                },
                headers={"User-Agent": self.USER_AGENT, "Content-Type": "application/x-www-form-urlencoded"},
            ) as resp:
                
                resp.raise_for_status()
                data = await resp.json()

        self.access_token  = data["access_token"]
        self.refresh_token = data.get("refresh_token", self.refresh_token)
        self.expires_at    = time.time() + data["expires_in"]
        print("[LINE] access_token更新 OK")
        return self.access_token

    async def get_token(self) -> str:
        if not self.access_token or time.time() > self.expires_at - 60:
            await self.refresh()
        return self.access_token

class LineGameAuth:
    AUTH_URL    = "https://game-api.line.me/auth/v3.8/refresh"
    APP_ID      = "LGTMTM"
    DEVICE_ID   = "ebf92ff8f267402fa5600f1a025242d9"
    SDK_VER     = "3.12.1.422"
    USER_AGENT  = "android;11;RQ3A_211001_001;GOOGLEPLAY;ja"

    def __init__(self, refresh_user_token: str = None):
        self.refresh_user_token = refresh_user_token
        self.user_token         = None
        self.user_key           = None
        self.expires_at         = 0

    async def refresh(self, access_token: str) -> dict:
        print("[GameSDK] userToken 更新中...")
        timestamp_ms = int(time.time() * 1000)
        ts_str = datetime.datetime.fromtimestamp(
            timestamp_ms / 1000,
            tz=datetime.timezone(datetime.timedelta(hours=9))
        ).strftime("%Y-%m-%dT%H:%M:%S.000+0900")

        body = msgpack.packb(
            {"providerId": "LINE", "accessToken": access_token, "country": "JP"},
            use_bin_type=True,
        )

        async with aiohttp.ClientSession(headers={}) as session:
            async with session.post(
                self.AUTH_URL,
                data=body,
                headers={
                    "Accept-Encoding":          "identity",
                    "X-Linegame-DeviceId":      self.DEVICE_ID,
                    "X-Linegame-AppId":         self.APP_ID,
                    "Content-Type":             "application/x-msgpack",
                    "X-Linegame-Authorization": f'ts="{timestamp_ms}", si=""',
                    "User-Agent":               self.USER_AGENT,
                    "X-Linegame-Timestamp":     ts_str,
                    "X-Linegame-SdkVersion":    self.SDK_VER,
                    "X-Linegame-MCC":           "",
                    "X-Linegame-MNC":           "",
                },
            ) as resp:
                print(f"[GameSDK] status: {resp.status}")
                resp.raise_for_status()
                data = await resp.json()

        self.user_key           = data["userKey"]
        self.user_token         = data["userToken"]
        self.refresh_user_token = data["refreshUserToken"]
        self.expires_at         = data["userTokenExpireTime"] / 1000
        print("[GameSDK] userToken取得 OK")
        return data

    async def get_user_token(self, access_token: str) -> str:
        if not self.user_token or time.time() > self.expires_at - 60:
            await self.refresh(access_token)
        return self.user_token

class LGTMTMClient:
    BASE_URL    = "https://lgtmtm-game.linegame.jp"
    APP_VER     = TSUM_APP_VER
    RES_VER     = TSUM_RES_VER
    MST_VER     = "1.0.0"
    HASH        = "4c7ad3ad6b44d908964a95f44fbcdb35cdf2354ad768888e155aa5ad10496157"
    TERMINAL_ID = "ebf92ff8f267402fa5600f1a025242d9"
    DEVICE_NAME = "Pixel 4a (5G)"

    def __init__(self, refresh_token: str, refresh_user_token: str = None, proxy: str = None):
        self.token_manager = LineTokenManager(refresh_token)
        self.game_auth     = LineGameAuth(refresh_user_token)
        self.user_id       = ""
        self.checkval      = ""
        self.session_id    = ""
        self.enc_key       = None
        self.proxy         = proxy

    @staticmethod
    def _generate_request_id() -> str:
        rand_part = random.randint(0, 0xF) << 28
        time_part = int(time.time()) & 0x0FFFFFFF
        return str((rand_part | time_part) or 1)

    def _growthyinfo(self) -> str:
        info = {
            "sdkVersion": "3.0", "osVer": "11",
            "terminalId": self.TERMINAL_ID, "deviceName": self.DEVICE_NAME,
            "country": "JP", "language": "ja", "networkType": 2,
            "carrier": "/", "clientTimestamp": datetime.datetime.now().strftime("%Y%m%d %H%M%S"),
        }
        return urllib.parse.quote(json.dumps(info, separators=(",", ":")))

    def _build_query(self, params: dict) -> str:
        return "&".join(f"{k}={v}" for k, v in params.items()) + f"&growthyinfo={self._growthyinfo()}"

    def _encrypt(self, plaintext: str) -> bytes:
        if not self.enc_key or len(self.enc_key) != 32:
            raise RuntimeError("SecureSessionが未確立です (AES key unavailable)")
        return SecureSessionCrypto.aes_gcm_encrypt(plaintext, self.enc_key)

    def _decrypt(self, data: bytes) -> dict:
        if not self.enc_key or len(self.enc_key) != 32:
            raise RuntimeError("SecureSessionが未確立です (AES key unavailable)")
        raw = SecureSessionCrypto.aes_gcm_decrypt(data, self.enc_key)

        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return dict(urllib.parse.parse_qsl(raw))

    def _update_session(self, resp: dict):
        if "hash" in resp and resp["hash"]:
            self.HASH = resp["hash"]

        userinfo = resp.get("userinfo", resp)

        checkval = userinfo.get("checkval")
        if checkval:
            self.checkval = checkval
            if TSUM_DEBUG:
                print("[*] checkval updated")

        session_id = (
            userinfo.get("session_id")
            or userinfo.get("sessionid")
            or userinfo.get("sessionId")
        )
        if session_id:
            self.session_id = session_id
            print("[*] session_id updated")

        user_id = userinfo.get("userid") or userinfo.get("userId")
        if user_id:
            self.user_id = user_id
            if TSUM_DEBUG:
                print("[*] userid updated")

    def _base_headers(self, extra: dict = None) -> dict:
        h = {"Accept": "*/*", "Content-Type": "application/tsum"}
        if self.session_id:
            h["X-Session-Id"] = self.session_id
        if extra:
            h.update(extra)
        return h

    def _base_params(self) -> dict:
        return {
            "requestid":   self._generate_request_id(),
            "userid":      self.user_id,
            "appver":      self.APP_VER,
            "resver":      self.RES_VER,
            "mstver":      self.MST_VER,
            "hash":        self.HASH,
            "os":          "2",
            "lang":        "ja",
            "checkval":    self.checkval,
            "countrycode": "JP",
        }

    async def _post_plain(self, endpoint: str, params: dict, extra_headers: dict = None) -> dict:
        plaintext = self._build_query(params)
        print(f"[>] POST {endpoint} (平文)")
        async with aiohttp.ClientSession() as session:
            async with session.post(
                self.BASE_URL + endpoint,
                data=plaintext,
                headers=self._base_headers(extra_headers),
                proxy=self.proxy,
            ) as resp:
                resp.raise_for_status()
                text = await resp.text()
        #print(f"[<] {text[:200]}")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return dict(urllib.parse.parse_qsl(text))

    async def _post(self, endpoint: str, params: dict) -> dict:
        plaintext = self._build_query(params)
        encrypted = self._encrypt(plaintext)
        print(f"[>] POST {endpoint}")

        is_gacha = endpoint in {
            "/gachaResult.nhn",
            "/gachaResultMulti.nhn",
            "/pickUpResult.nhn",
        }
        gacha_diag = is_gacha and (TSUM_GACHA_DEBUG or GACHA_DIAGNOSTIC_FORCE)

        async with aiohttp.ClientSession() as session:
            async with session.post(
                self.BASE_URL + endpoint,
                data=encrypted,
                headers=self._base_headers(),
                proxy=self.proxy,
            ) as resp:
                content = await resp.read()
                if gacha_diag:
                    print(
                        f"[GACHA-HTTP] endpoint={endpoint} "
                        f"status={resp.status} bytes={len(content)}"
                    )
                if resp.status >= 400:
                    if gacha_diag:
                        print(f"[GACHA-HTTP-ERROR] status={resp.status} reason={resp.reason}")
                    resp.raise_for_status()

        try:
            result = self._decrypt(content)
        except Exception as e:
            if gacha_diag:
                print(f"[GACHA-DECRYPT-ERROR] {type(e).__name__}: {e}")
            raise

        if gacha_diag:
            user = result.get("userinfo") or {}
            print(
                "[GACHA] "
                f"endpoint={endpoint} "
                f"request_gachaid={params.get('gachaid')} "
                f"retcode={result.get('retcode')} "
                f"retsubcode={result.get('retsubcode')} "
                f"bcoin={user.get('bcoin')}"
            )

            gacha_keys = [k for k in result.keys() if "gacha" in str(k).lower()]
            print(f"[GACHA] response_gacha_keys={gacha_keys}")
            for key in gacha_keys:
                try:
                    rendered = json.dumps(result.get(key), ensure_ascii=False, separators=(",", ":"))
                except Exception:
                    rendered = repr(result.get(key))
                if len(rendered) > 1800:
                    rendered = rendered[:1800] + "...<truncated>"
                print(f"[GACHA] {key}={rendered}")

        if(endpoint=="/getMast.nhn"):
            with RESULT_FILE.open("w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, indent=2)
            #print(f"[<]\n{json.dumps(result, ensure_ascii=False)}")
        if TSUM_DEBUG and endpoint in {"/gameStart.nhn", "/gameEnd.nhn", "/getInfo.nhn"}:
            print(f"[<]\n{json.dumps(result, ensure_ascii=False)}")
        self._update_session(result)
        return result
    
    async def _get_public_key(self) -> tuple[str, str]:
        key_resp = await self._post_plain(
            f"/api/getPublicKey.nhn?appver={self.APP_VER}",
            {
                "requestid": self._generate_request_id(),
                "userid": "",
                "appver": self.APP_VER,
                "resver": self.RES_VER,
                "mstver": self.MST_VER,
                "hash": self.HASH,
                "os": "2",
                "lang": "ja",
                "checkval": "",
                "countrycode": "JP",
            },
        )

        public_key_b64 = (
            key_resp.get("public_key")
            or key_resp.get("publickey")
            or key_resp.get("key")
        )
        key_id = key_resp.get("kid")

        if not public_key_b64:
            raise RuntimeError("getPublicKey: public_key がありません")
        if not key_id:
            raise RuntimeError("getPublicKey: kid がありません")

        print("[3] getPublicKey OK")
        print(f"    kid: {key_id}")
        return public_key_b64, key_id

    def _establish_secure_session(self, public_key_b64: str) -> str:
        session_key = SecureSessionCrypto.generate_secure_random(32)
        if len(session_key) != 32:
            raise RuntimeError("SecureSession: 32byte key generation failed")

        encrypted_session_key = SecureSessionCrypto.rsa_encrypt_oaep(
            session_key,
            public_key_b64,
        )

        self.enc_key = session_key
        print("[4] SecureSession key + RSA-OAEP OK")
        if TSUM_DEBUG:
            print(f"    AES key length: {len(self.enc_key)} bytes")
        return encrypted_session_key

    async def _game_login(
        self,
        user_token: str,
        key_id: str,
        encrypted_session_key: str,
    ) -> dict:
        login_params = {
            "rankdt": "0",
            "accesstoken": urllib.parse.quote(user_token, safe=""),
            "trigger": "0",
            "userid": "",
            "appver": self.APP_VER,
            "resver": self.RES_VER,
            "mstver": self.MST_VER,
            "hash": self.HASH,
            "os": "2",
            "lang": "ja",
            "checkval": "",
            "countrycode": "JP",
        }

        plaintext = self._build_query(login_params)
        encrypted = self._encrypt(plaintext)

        print("[5] login.nhn request")
        async with aiohttp.ClientSession() as session:
            async with session.post(
                self.BASE_URL + "/login.nhn",
                data=encrypted,
                headers={
                    "Accept": "*/*",
                    "Content-Type": "application/tsum",
                    "X-Encrypted-Session-Key": encrypted_session_key,
                    "X-Key-Id": key_id,
                },
                proxy=self.proxy,
            ) as resp:
                print(f"[6] login.nhn status: {resp.status}")
                resp.raise_for_status()
                content = await resp.read()

        try:
            result = self._decrypt(content)
        except Exception as e:
            raise RuntimeError(f"login response decrypt failed: {type(e).__name__}") from e

        print("[7] login response decrypt OK")
        self._update_session(result)

        if self.session_id:
            print("[8] SecureSession established: session_id OK")
        else:
            print("[8] WARNING: login応答からsession_idを確認できません")

        return result

    async def login(self) -> dict:
        print("=" * 50)
        print(f"[*] client appver={self.APP_VER} resver={self.RES_VER}")

        print("[1] LINE access_token")
        access_token = await self.token_manager.get_token()
        if not access_token:
            raise RuntimeError("LINE access_token が取得できません")
        print("[1] LINE access_token OK")

        print("[2] game-api userToken")
        game_data = await self.game_auth.refresh(access_token)
        user_token = game_data.get("userToken")
        if not user_token:
            raise RuntimeError("GameSDK userToken が取得できません")
        print("[2] game-api userToken OK")

        public_key_b64, key_id = await self._get_public_key()
        encrypted_session_key = self._establish_secure_session(public_key_b64)
        return await self._game_login(user_token, key_id, encrypted_session_key)

    
    async def get_item_store(self) -> dict:
        params = self._base_params()
        return await self._post("/getItemStore.nhn", {"requestid": params.pop("requestid"), **params})

    async def get_plaza(self) -> dict:
        params = self._base_params()
        return await self._post("/getPlaza.nhn", {"requestid": params.pop("requestid"), **params})

    async def get_information(self) -> dict:
        params = self._base_params()
        return await self._post("/getInformation.nhn", {"requestid": params.pop("requestid"), **params})

    async def get_mast(self, kind: list[int] | None = None) -> dict:
        if kind is None: kind = [0]
        params = self._base_params()
        return await self._post("/getMast.nhn", {
            "requestid": params.pop("requestid"),
            "kind": "|".join(map(str, kind)) + "|",
            **params,
        })
    
    async def get_event_id(self):
        
        now = int(time.time())
        data= await self.get_mast([3,7,11,14])

        for e in data.get("eventmst", []):
            start = int(e["startdt"])
            end = int(e["enddt"])
            etype = e.get("type")

            if start <= now <= end and etype == 133:
                return e["eventid"]

        return 0

    async def get_info(self, kind: int = -1, limitedruby: int = 0, mytsumid: int | None = None) -> dict:
        params = self._base_params()
        ordered = {
            "requestid":   params.pop("requestid"),
            **params,
        }
        return await self._post("/getInfo.nhn", ordered)

    async def ads(self, cmd: int = 0) -> dict:
        params = self._base_params()
        return await self._post("/ads.nhn", {"requestid": params.pop("requestid"), "cmd": cmd, **params})

    async def pickup_result(self, gacha_id: int) -> dict:
        """現行SceneStoreのtype=21/31経路。通常購入ではtargetmidを送らない。"""
        params = self._base_params()
        return await self._post("/pickUpResult.nhn", {
            "requestid": params.pop("requestid"), "gachaid": gacha_id, **params,
        })

    async def gacha_result_multi(
        self, gacha_id: int, bonus_id: int = 0, pay_type: int = 1
    ) -> dict:
        params = self._base_params()
        payload = {
            "requestid": params.pop("requestid"),
            "gachaid": gacha_id,
            "paytype": str(int(pay_type)),
            **params,
        }
        if bonus_id >= 1:
            payload["gachabonusid"] = int(bonus_id)
        return await self._post("/gachaResultMulti.nhn", payload)

    async def gacha_result(
        self, gacha_id: int, bonus_id: int = 0, pay_type: int = 1
    ) -> dict:
        params = self._base_params()
        payload = {
            "requestid": params.pop("requestid"),
            "gachaid": gacha_id,
            "paytype": str(int(pay_type)),
            **params,
        }
        if bonus_id >= 1:
            payload["gachabonusid"] = int(bonus_id)
        return await self._post("/gachaResult.nhn", payload)
    async def is_event_joined(self,event):
        eventinfo = event.get("eventinfo", {})
        return eventinfo.get("card", 0) > 0
    async def game_start(self, tsum_id: int) -> dict:
        params = self._base_params()
        id=0
        id=await self.get_event_id()
        if(await self.is_event_joined(await self.get_event(id))==False):
            id=0
        return await self._post("/gameStart.nhn", {
            "requestid": params.pop("requestid"),
            "tsumid": tsum_id, "eventid":id, "plazaeventid": 0,
            "hearttype": 0, "probmstver": 2, "bonusflg": 0,
            **params,
        })

    async def game_end(self,tsum_id:int, score: int = 0, coin: int = 0, exp: int = 0,
                       medal: int = 0, play_code: str = None) -> dict:
        def _build_other_result(coin: int) -> str:
            obj = {"playlog": {"coin": {
                "inval": [], "cnt": {"1": coin}, "key": [1], "sum": str(coin), "req": "ne",
            }}}
            return urllib.parse.quote(json.dumps(obj, separators=(",", ":")), safe="")

        params = self._base_params()
        return await self._post("/gameEnd.nhn", {
            "requestid": params.pop("requestid"),
            "score": score, "coin": coin, "medal": medal, "exp": exp,
            "combo": 1, "chain": 6, "treasurecnt": 0, "fever": 0,
            "skillcnt": 0, "extendflg": 0, "basescore": 2580, "basecoin": 5,
            "basemedal": medal, "maxmedal": medal, "baseexp": 100, "playtime": 60,
            "vanishcnt": f"{tsum_id},10|1,0|2,0|3,0|4,0|", "playcode": play_code,
            "bombcnt": "0,0|1,0|2,0|3,0|4,0|5,0|6,0|", "cheat": 0,
            "sizecnt": "0,6|1,0|2,0|", "eventpt": "", "boxcnt": "",
            "genericval": "", "otherresult": _build_other_result(coin),
            **params,
        })
    
    


    async def game_play(self,info: GetInfoResponse = None,tsum_id: int=1,score:int=0,coin:int=0,exp:int=0,medal:int=0) -> dict | int:
        data = await self.game_start(tsum_id)
        try:
            if data.get("retcode", -1) != 0:
                print(f"エラー (retcode: {data.get('retcode')})")
                return 0

            user_info = data.get("userinfo", {})
            playcode  = user_info.get("playcode")
            print(f"PlayCode={playcode}, Coin={user_info.get('bcoin')}, Heart={user_info.get('bheart')}")

            res_end  = await self.game_end(tsum_id=tsum_id,score=score,coin=coin,exp=exp,medal=medal,play_code=playcode)
            end_ret  = res_end.get("retcode", -1)
            if end_ret == 0:
                print("[SUCCESS] game_end成功")
                return res_end
            else:
                print(f"[END ERROR] retcode={end_ret}")
                return 0
        except Exception as e:
            print(f"エラー: {e}")
            return 0
    async def release_tsum_lv(self, tsumid: int) -> dict:
        """POST /releaseTsumLv.nhn"""
        params = self._base_params()
        ordered = {
            "requestid": params.pop("requestid"),
            "tsumid":    tsumid,
            **params,
        }
        return await self._post("/releaseTsumLv.nhn", ordered)
    

    async def gacha_comp(
        self,
        gacha_id: int,
        pickup: bool = False,
        multi_allowed: bool | None = None,
        bonus_id: int = 0,
    ):
        """ガチャを完売方向へ進める。

        現行getInfoの multiflg を呼び出し側から渡し、
        multiflg=0 のガチャに gachaResultMulti を送らない。
        """
        print(
            f"[GACHA-COMP] gachaid={gacha_id} pickup={pickup} "
            f"multi_allowed={multi_allowed} bonus_id={bonus_id}"
        )

        async def wait_loop(func, end_codes, max_loop=500):
            for loop_no in range(1, max_loop + 1):
                try:
                    # pickupResult は bonus_id を受けないので分岐する。
                    func_name = getattr(func, "__name__", "")
                    if func_name == "pickup_result":
                        res = await func(gacha_id=gacha_id)
                    else:
                        res = await func(gacha_id=gacha_id, bonus_id=bonus_id)
                except Exception as e:
                    print(
                        f"[GACHA-CALL-ERROR] loop={loop_no} "
                        f"func={getattr(func, '__name__', str(func))} "
                        f"{type(e).__name__}: {e}"
                    )
                    return {
                        "retcode": -998,
                        "retmsg": f"{type(e).__name__}: {e}",
                    }

                if not isinstance(res, dict):
                    print(f"[GACHA-RESULT-ERROR] loop={loop_no} non-dict response: {type(res).__name__}")
                    return {"retcode": -997, "retmsg": "non-dict response"}

                retcode = res.get("retcode")
                if retcode is None:
                    print(f"[GACHA-RESULT-ERROR] loop={loop_no} retcode missing keys={list(res.keys())}")
                    return {"retcode": -996, "retmsg": "retcode missing"}

                print(
                    f"[GACHA-LOOP] loop={loop_no} "
                    f"func={getattr(func, '__name__', str(func))} retcode={retcode}"
                )

                coin = res.get("userinfo", {}).get("bcoin")
                if coin is not None and coin < 300_000:
                    return {
                        "retcode": -1,
                        "retmsg": f"コイン不足で停止 ({coin:,})"
                    }

                if retcode in end_codes:
                    return res

                if retcode != 0:
                    return res

            return {"retcode": -1, "retmsg": "timeout"}

        if pickup:
            return await wait_loop(self.pickup_result, (31, 34, 35))

        if multi_allowed is False:
            return await wait_loop(self.gacha_result, (31, 34, 35))

        res = await wait_loop(self.gacha_result_multi, (34, 35))

        if res.get("retcode") == 35:
            res = await wait_loop(self.gacha_result, (31, 34, 35))

        return res
    @staticmethod
    def _gacha_completed_in_response(res: dict, gacha_id: int) -> bool:
        comp = res.get("gachacompinfo") if isinstance(res, dict) else None
        entries = comp if isinstance(comp, list) else [comp] if isinstance(comp, dict) else []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            if entry.get("gachaid") == gacha_id and entry.get("compflg") == 1:
                return True
        return False

    @staticmethod
    def _gacha_bonus_id_from_response(res: dict, current: int = 0) -> int:
        info = res.get("gachabonusinfo") if isinstance(res, dict) else None
        entries = info if isinstance(info, list) else [info] if isinstance(info, dict) else []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            value = entry.get("id")
            if isinstance(value, int):
                return value
        return current

    @staticmethod
    def _gacha_ticket_count_from_response(
        res: dict, gacha_type: int, current: int
    ) -> tuple[int, bool]:
        """レスポンスにtype=7/id=gachaTypeのticket情報があれば更新する。"""
        if not isinstance(res, dict):
            return current, False
        found = False
        value = current
        for key in ("iteminfo", "ticketinfo"):
            raw = res.get(key)
            entries = raw if isinstance(raw, list) else [raw] if isinstance(raw, dict) else []
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                if entry.get("type") == 7 and entry.get("id") == gacha_type:
                    cnt = entry.get("cnt")
                    if isinstance(cnt, int):
                        value = max(0, cnt)
                        found = True
        return value, found

    @staticmethod
    def _gacha_currency(gacha_type: int) -> tuple[str, str]:
        """SceneStore::purchaseProcedureのuType % 10分岐を再現。"""
        mod = int(gacha_type) % 10
        if mod == 1:
            return "coin", "コイン"
        if mod == 2:
            return "ruby", "ルビー"
        if mod == 4:
            return "medal", "メダル"
        return "unknown", "不明通貨"

    @staticmethod
    def _gacha_balance_from_response(
        res: dict, currency: str, current: int
    ) -> tuple[int, bool]:
        ui = res.get("userinfo") if isinstance(res, dict) else None
        if not isinstance(ui, dict):
            return current, False
        key_map = {
            "coin": ("bcoin", "pcoin"),
            "ruby": ("bruby", "pruby"),
            "medal": ("bmedal", "pmedal"),
        }
        keys = key_map.get(currency)
        if not keys:
            return current, False
        if not any(k in ui for k in keys):
            return current, False
        total = 0
        for key in keys:
            value = ui.get(key, 0)
            if isinstance(value, int):
                total += value
        return max(0, total), True

    async def gacha_comp_store(
        self,
        gacha_id: int,
        gacha_type: int,
        multi_allowed: bool,
        curr_price: int,
        balance: int,
        ticket_count: int,
        bonus_id: int = 0,
        label: str = "STORE",
        max_calls: int | None = None,
        max_draws: int = 0,
    ) -> dict:
        """現行SceneStoreのtype=21/31経路
        """
        max_calls = max(1, min(max_calls or TSUM_GACHA_MAX_CALLS, TSUM_GACHA_MAX_CALLS))
        price = max(0, int(curr_price or 0))
        balance = max(0, int(balance or 0))
        ticket_count = max(0, int(ticket_count or 0))
        bonus_id = max(0, int(bonus_id or 0))
        max_draws = max(0, int(max_draws or 0))
        currency, currency_jp = self._gacha_currency(gacha_type)
        if currency == "unknown":
            return {
                "retcode": -990,
                "_status": "unsupported_currency",
                "retmsg": f"未対応のGachaType={gacha_type}",
            }

        draws = 0
        print(
            f"[{label}-NATIVE] gachaid={gacha_id} type={gacha_type} "
            f"currency={currency} price={price} balance={balance} "
            f"tickets={ticket_count} bonus_id={bonus_id} multi_allowed={multi_allowed} "
            f"max_calls={max_calls} max_draws={max_draws or 'unbounded'}"
        )

        last_res: dict = {"retcode": 0}
        for call_no in range(1, max_calls + 1):
            remaining = max_draws - draws if max_draws > 0 else None

            can_try_multi = bool(multi_allowed and (remaining is None or remaining >= 10))
            if can_try_multi and ticket_count >= 10:
                use_multi, units, pay_type = True, 10, 2
            elif can_try_multi and (price == 0 or balance >= price * 10):
                use_multi, units, pay_type = True, 10, 1
            elif ticket_count >= 1:
                use_multi, units, pay_type = False, 1, 2
            elif price == 0 or balance >= price:
                use_multi, units, pay_type = False, 1, 1
            else:
                print(
                    f"[{label}-STOP] {currency_jp}不足: balance={balance} "
                    f"need={price} tickets={ticket_count}"
                )
                return {
                    "retcode": -2,
                    "_status": "insufficient_funds",
                    "_currency": currency,
                    "_currency_jp": currency_jp,
                    "_balance": balance,
                    "_need": price,
                    "_ticket_count": ticket_count,
                    "_calls": call_no - 1,
                    "_draws": draws,
                    "_bonus_id": bonus_id,
                    "retmsg": f"{currency_jp}不足 ({balance:,} / 必要 {price:,})",
                }

            cost = price * units if pay_type == 1 else 0
            mode = "MULTI" if use_multi else "SINGLE"
            print(
                f"[{label}-{mode}] call={call_no} draws={draws} "
                f"paytype={pay_type} units={units} cost={cost} "
                f"balance={balance} tickets={ticket_count} bonus_id={bonus_id}"
            )

            try:
                if use_multi:
                    res = await self.gacha_result_multi(
                        gacha_id=gacha_id,
                        bonus_id=bonus_id,
                        pay_type=pay_type,
                    )
                else:
                    res = await self.gacha_result(
                        gacha_id=gacha_id,
                        bonus_id=bonus_id,
                        pay_type=pay_type,
                    )
            except Exception as e:
                return {
                    "retcode": -998,
                    "_status": "exception",
                    "_calls": call_no,
                    "_draws": draws,
                    "retmsg": f"{type(e).__name__}: {e}",
                }

            if not isinstance(res, dict):
                return {
                    "retcode": -997,
                    "_status": "invalid_response",
                    "retmsg": "non-dict response",
                }
            last_res = dict(res)
            retcode = res.get("retcode")
            print(
                f"[{label}-RESULT] call={call_no} mode={mode} "
                f"retcode={retcode} retsubcode={res.get('retsubcode')}"
            )

            if retcode == 35 and use_multi:
                print(f"[{label}-FALLBACK] retcode=35 -> Singleへ切替")
                multi_allowed = False
                continue
            if retcode != 0:
                last_res.update({
                    "_status": "server_error",
                    "_calls": call_no,
                    "_draws": draws,
                    "_balance": balance,
                    "_ticket_count": ticket_count,
                    "_bonus_id": bonus_id,
                })
                return last_res

            draws += units
            bonus_id = self._gacha_bonus_id_from_response(res, bonus_id)

            new_balance, balance_found = self._gacha_balance_from_response(
                res, currency, balance
            )
            if balance_found:
                balance = new_balance
            elif pay_type == 1:
                balance = max(0, balance - cost)

            new_tickets, ticket_found = self._gacha_ticket_count_from_response(
                res, gacha_type, ticket_count
            )
            if ticket_found:
                ticket_count = new_tickets
            elif pay_type == 2:
                ticket_count = max(0, ticket_count - units)

            if self._gacha_completed_in_response(res, gacha_id):
                print(f"[{label}-COMPLETE] compflg=1 calls={call_no} draws={draws}")
                last_res.update({
                    "_status": "completed",
                    "_completed": True,
                    "_calls": call_no,
                    "_draws": draws,
                    "_balance": balance,
                    "_ticket_count": ticket_count,
                    "_bonus_id": bonus_id,
                })
                return last_res

            if max_draws > 0 and draws >= max_draws:
                last_res.update({
                    "_status": "max_draws_reached",
                    "_calls": call_no,
                    "_draws": draws,
                    "_balance": balance,
                    "_ticket_count": ticket_count,
                    "_bonus_id": bonus_id,
                })
                return last_res

        last_res.update({
            "retcode": -1,
            "_status": "loop_limit",
            "_calls": max_calls,
            "_draws": draws,
            "_balance": balance,
            "_ticket_count": ticket_count,
            "_bonus_id": bonus_id,
            "retmsg": f"安全上限 {max_calls} 回に到達",
        })
        return last_res

    async def gacha_comp_pickup(
        self,
        gacha_id: int,
        gacha_type: int,
        curr_price: int,
        balance: int,
        ticket_count: int,
        max_count: int = 0,
        label: str = "PICKUP",
        max_calls: int | None = None,
    ) -> dict:
        """type=21/31のSceneStore経路を再現。/pickUpResult.nhnを1回ずつ呼ぶ。"""
        configured = max_calls or TSUM_GACHA_MAX_CALLS
        if max_count and max_count > 0:
            # transient/no-op 応答用に少し余裕を持たせる。
            configured = min(configured, max_count + 10)
        max_calls = max(1, min(configured, TSUM_GACHA_MAX_CALLS))
        price = max(0, int(curr_price or 0))
        balance = max(0, int(balance or 0))
        ticket_count = max(0, int(ticket_count or 0))
        currency, currency_jp = self._gacha_currency(gacha_type)
        if currency == "unknown":
            return {"retcode": -990, "_status": "unsupported_currency"}

        print(
            f"[{label}-NATIVE] gachaid={gacha_id} type={gacha_type} "
            f"endpoint=/pickUpResult.nhn price={price} {currency}={balance} "
            f"tickets={ticket_count} maxcnt={max_count} max_calls={max_calls}"
        )

        last_res: dict = {"retcode": 0}
        draws = 0
        no_progress_streak = 0
        for call_no in range(1, max_calls + 1):
            if max_count > 0 and draws >= max_count:
                last_res.update({
                    "_status": "max_draws_reached",
                    "_calls": call_no - 1,
                    "_draws": draws,
                    "_balance": balance,
                    "_ticket_count": ticket_count,
                })
                return last_res
            # purchaseProcedureはPickupへ分岐する前にticket/currency不足を判定する。
            uses_ticket = ticket_count >= 1
            if not uses_ticket and price > 0 and balance < price:
                print(
                    f"[{label}-STOP] {currency_jp}不足: balance={balance} "
                    f"need={price} tickets={ticket_count}"
                )
                return {
                    "retcode": -2,
                    "_status": "insufficient_funds",
                    "_currency": currency,
                    "_currency_jp": currency_jp,
                    "_balance": balance,
                    "_need": price,
                    "_ticket_count": ticket_count,
                    "_calls": call_no - 1,
                    "_draws": draws,
                    "retmsg": f"{currency_jp}不足 ({balance:,} / 必要 {price:,})",
                }

            print(
                f"[{label}-SINGLE] call={call_no} draws={draws} "
                f"payment={'ticket' if uses_ticket else currency} "
                f"balance={balance} tickets={ticket_count}"
            )
            try:
                res = await self.pickup_result(gacha_id=gacha_id)
            except Exception as e:
                return {
                    "retcode": -998,
                    "_status": "exception",
                    "_calls": call_no,
                    "_draws": draws,
                    "retmsg": f"{type(e).__name__}: {e}",
                }
            if not isinstance(res, dict):
                return {"retcode": -997, "_status": "invalid_response"}
            last_res = dict(res)
            retcode = res.get("retcode")
            print(
                f"[{label}-RESULT] call={call_no} retcode={retcode} "
                f"retsubcode={res.get('retsubcode')}"
            )
            if retcode != 0:
                last_res.update({
                    "_status": "server_error",
                    "_calls": call_no,
                    "_draws": draws,
                    "_balance": balance,
                    "_ticket_count": ticket_count,
                })
                return last_res

            prev_balance = balance
            prev_tickets = ticket_count

            new_balance, balance_found = self._gacha_balance_from_response(
                res, currency, balance
            )
            if balance_found:
                balance = new_balance

            new_tickets, ticket_found = self._gacha_ticket_count_from_response(
                res, gacha_type, ticket_count
            )
            if ticket_found:
                ticket_count = new_tickets

            completed = self._gacha_completed_in_response(res, gacha_id)

            if price == 0:
                progressed = True
            elif uses_ticket:
                progressed = ticket_found and ticket_count < prev_tickets
            else:
                progressed = balance_found and balance < prev_balance

            # サーバーが残高/チケットを返さない旧互換時のみローカル推定する。
            if not balance_found and not ticket_found:
                if uses_ticket and ticket_count > 0:
                    ticket_count = max(0, ticket_count - 1)
                    progressed = True
                elif not uses_ticket and price > 0 and balance >= price:
                    balance = max(0, balance - price)
                    progressed = True

            if progressed:
                draws += 1
                no_progress_streak = 0
                print(
                    f"[{label}-PROGRESS] call={call_no} draws={draws} "
                    f"balance={balance} tickets={ticket_count}"
                )
            else:
                no_progress_streak += 1
                print(
                    f"[{label}-NO-PROGRESS] call={call_no} retcode=0 "
                    f"balance={balance} tickets={ticket_count} "
                    f"streak={no_progress_streak}"
                )

            if completed:
                print(f"[{label}-COMPLETE] compflg=1 calls={call_no} draws={draws}")
                last_res.update({
                    "_status": "completed",
                    "_completed": True,
                    "_calls": call_no,
                    "_draws": draws,
                    "_balance": balance,
                    "_ticket_count": ticket_count,
                })
                return last_res

            if no_progress_streak >= 3:
                last_res.update({
                    "_status": "no_progress",
                    "_calls": call_no,
                    "_draws": draws,
                    "_balance": balance,
                    "_ticket_count": ticket_count,
                    "retmsg": "retcode=0だが購入成立を3回連続で確認できませんでした",
                })
                return last_res

        last_res.update({
            "_status": "call_limit",
            "_calls": max_calls,
            "_draws": draws,
            "_balance": balance,
            "_ticket_count": ticket_count,
            "retmsg": f"HTTP呼び出し安全上限 {max_calls} 回に到達",
        })
        return last_res

    async def gacha_comp_h(self, gacha_id: int = 104, bonus_id: int = 0):
        

        def completed_in_response(res: dict) -> bool:
            comp = res.get("gachacompinfo")
            entries = comp if isinstance(comp, list) else [comp] if isinstance(comp, dict) else []
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                if entry.get("gachaid") == gacha_id and entry.get("compflg") == 1:
                    return True
            return False

        print(
            f"[HAPPINESS-COMP] gachaid={gacha_id} "
            f"single=True bonus_id={bonus_id}"
        )

        for loop_no in range(1, 1001):
            try:
                res = await self.gacha_result(
                    gacha_id=gacha_id,
                    bonus_id=bonus_id,
                )
            except Exception as e:
                print(f"[HAPPINESS-CALL-ERROR] loop={loop_no} {type(e).__name__}: {e}")
                return {"retcode": -998, "retmsg": f"{type(e).__name__}: {e}"}

            if not isinstance(res, dict):
                return {"retcode": -997, "retmsg": "non-dict response"}

            retcode = res.get("retcode")
            print(f"[HAPPINESS-LOOP] loop={loop_no} retcode={retcode}")

            if completed_in_response(res):
                print("[HAPPINESS-COMP] compflg=1 detected")
                return res

            if retcode in (31, 34, 35):
                return res

            if retcode != 0:
                return res

        return {"retcode": -1, "retmsg": "ループ上限到達"}
    async def buy_heart(self, setid: int = 11) -> dict:
        """POST /buyHeart.nhn"""
        params = self._base_params()
        ordered = {
            "requestid": params.pop("requestid"),
            "setid":     setid,
            **params,
        }
        return await self._post("/buyHeart.nhn", ordered)
    async def get_event(self, eventid: int, updateflg: int = 0) -> dict:
        """POST /getEvent.nhn"""
        params = self._base_params()

        ordered = {
            "requestid": params.pop("requestid"),
            "eventid": eventid,
            "updateflg": updateflg,
            **params,
        }

        return await self._post("/getEvent.nhn", ordered)
    
    async def tsum_level(self,tsum_id:int):
        
        for i in range(6):
            await self.game_play(tsum_id=tsum_id)
            await self.release_tsum_lv(tsumid=tsum_id)

    async def player_level(self):
        return await self.game_play(exp=100000000)
    def extract_gacha_ids(self, gachamst):
        gacha_type_map = {
            1: "happiness",
            11: "premium",
            21: "pickup",
            31: "pickup_tower",
            41: "select",
            53: "invite",
            64: "premiumplus",
        }

        result = {}
        for g in gachamst or []:
            if not isinstance(g, dict):
                continue
            name = gacha_type_map.get(g.get("type"))
            gacha_id = g.get("gachaid")
            if name and isinstance(gacha_id, int):
                result.setdefault(name, []).append(gacha_id)

        return result

    def resolve_active_gacha_targets(self, gachamst, available_gachas):
        type_names = {
            1: "happiness",
            11: "premium",
            21: "pickup",
            31: "pickup_tower",
            41: "select",
            53: "invite",
            64: "premiumplus",
        }
        available_by_id = {g.gachaid: g for g in available_gachas}

        resolved = {}
        for row in gachamst or []:
            if not isinstance(row, dict):
                continue
            name = type_names.get(row.get("type"))
            gacha_id = row.get("gachaid")
            if not name or not isinstance(gacha_id, int):
                continue
            info = available_by_id.get(gacha_id)
            if info is None or name in resolved:
                continue

            list_price = row.get("listprice", row.get("price", 0))
            curr_price = row.get("currprice", 0)
            if not isinstance(list_price, int):
                list_price = 0
            if not isinstance(curr_price, int):
                curr_price = 0
            effective_price = curr_price if curr_price > 0 else list_price

            resolved[name] = {
                "gachaid": gacha_id,
                "type": row.get("type"),
                "name": row.get("name", ""),
                "maxcnt": row.get("maxcnt"),
                "listprice": list_price,
                "currprice": curr_price,
                "price": effective_price,
                "multiflg": info.multiflg,
                "compflg": info.compflg,
            }

        return resolved
    
    
async def main():
    oauth = LineOAuthLogin(
        line_id  = "",
        password = "",
    )
    access_token, refresh_token = await oauth.login()
    print("refresh_token: [取得済み]")
    client = LGTMTMClient(refresh_token=refresh_token,proxy="")
    await client.login()



    

    res  = await client.get_info()
    info = GetInfoResponse.from_dict(res)
    print("ハート数:", info.userinfo.bheart)
    print("マイツム:", info.mytsum.tsumid)
    print("コイン:",info.userinfo.bcoin)
    #await client.gacha_comp(gacha_id=12004006,pickup=0)
    if (info.userinfo.bheart < 1):
        await client.buy_heart()


    
    
    res=await client.get_mast([3,7,11,14])
    
   
    gachamst=res["gachamst"]
    

    print(client.extract_gacha_ids(gachamst)['select'])
    pickup=client.extract_gacha_ids(gachamst)['select']



    #await client.pickup_result(pickup)
    #await client.gacha_comp(select,False)
    #await client.gacha_comp_h(hapiness)
    #await client.gacha_result(hapiness)

    
    if info.userinfo.bheart >= 1:
        #pass
        await client.game_play(info=info,tsum_id=info.mytsum.tsumid,coin=10)
        #await client.release_tsum_lv(950)

        #await client.gacha_comp(12004012)

    else:
        print("ハートを購入してください")
        return



    #await client.game_play(info=info, tsum_id=950)

    #await client.tsum_level(tsum_id=950)
 # ユーザー基本情報
    '''print(info.userinfo.bcoin)        # 188891183
    print(info.userinfo.bruby)        # 1432

    # マイツム
    print(info.mytsum.tsumid)         # 926
    print(info.mytsum.skilllv)        # 1

    # ツム個別取得
    tsum = info.get_tsum(950)
    print(tsum.wappen)                # [0, 0, -1]

    # アイテム残数
    print(info.play_items)            # {'5to4': 4, '4to3': 0, ...}
    print(info.get_item(11, 7))       # 5

    # スコアランキング
    for s in info.mydatainfo.tsumscorelist:
        print(s.tsumid, s.score)'''

if __name__ == "__main__":
    asyncio.run(main())
