import asyncio
import io
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import discord

from api2 import GetInfoResponse, LGTMTMClient, LineOAuthLogin

BASE_DIR = Path(__file__).resolve().parent
FREE_FILE = BASE_DIR / "free_users.json"
PANEL_CONFIG_FILE = BASE_DIR / "tsum_panel_configs.json"

try:
    LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "0") or "0")
except ValueError:
    LOG_CHANNEL_ID = 0

# 有料処理は完全削除。表示・計算上も全て0円。
MENUS = {
    "menu8": {
        "label": "5000万コイン",
        "price": 0,
        "desc": "5000万コイン処理",
    },
    "menu1": {
        "label": "1億コイン",
        "price": 0,
        "desc": "300円",
    },
    "menu7": {
        "label": "2億コイン",
        "price": 0,
        "desc": "500円",
    },
    "menu6": {
            "label": "プレイヤーレベルMAX",
            "price": 0,
            "desc": "400円",
        },
    "menu3": {
        "label": "プレミアムBOX完売",
        "price": 0,
        "desc": "800円",
    },
    "menu4": {
        "label": "ハピネスBOX完売",
        "price": 0,
        "desc": "300円",
    },
    "menu5": {
        "label": "ピックアップガチャ完売",
        "price": 0,
        "desc": "800円",
    },
    "menu9": {
        "label": "プレミアムBOX+完売",
        "price": 0,
        "desc": "800円",
    },
    "menu10": {
            "label": "セレクトボックス完売",
            "price": 0,
            "desc": "500円",
    },
    "menu11": {
                "label": "永久コイン補充",
                "price": 0,
                "desc": "4000円",
    },
    "menu12": {
                "label": "BAN保証",
                "price": 0,
                "desc": "500円",
    },
}

# 同一ユーザーの二重実行防止
_ACTIVE_USERS: set[int] = set()


def load_tsum_panel_configs() -> list[dict[str, int | str]]:
    """再起動後に常設パネルを復元するための、機密情報を含まない設定だけを読む。"""
    if not PANEL_CONFIG_FILE.exists():
        return []
    try:
        with PANEL_CONFIG_FILE.open("r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []

    if not isinstance(raw, list):
        return []

    configs: list[dict[str, int | str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        panel_id = item.get("panel_id")
        role_id = item.get("role_id")
        category_id = item.get("category_id")
        if isinstance(panel_id, str) and isinstance(role_id, int) and isinstance(category_id, int):
            configs.append(
                {
                    "panel_id": panel_id,
                    "role_id": role_id,
                    "category_id": category_id,
                }
            )
    return configs


def save_tsum_panel_config(panel_id: str, role_id: int, category_id: int) -> None:
    """常設パネルの送信先だけを保存する。ログイン情報等は保存しない。"""
    configs = load_tsum_panel_configs()
    configs = [config for config in configs if config["panel_id"] != panel_id]
    configs.append(
        {
            "panel_id": panel_id,
            "role_id": role_id,
            "category_id": category_id,
        }
    )
    with PANEL_CONFIG_FILE.open("w", encoding="utf-8") as f:
        json.dump(configs, f, ensure_ascii=False, indent=2)


def new_panel_id() -> str:
    return uuid.uuid4().hex


def _member_has_role(interaction: discord.Interaction, role_id: int) -> bool:
    member = interaction.user
    return isinstance(member, discord.Member) and member.get_role(role_id) is not None


async def _reject_unauthorized(interaction: discord.Interaction) -> None:
    if not interaction.response.is_done():
        await interaction.response.send_message(
            "❌ この操作を実行する権限がありません。",
            ephemeral=True,
        )


def _ticket_channel_name(display_name: str) -> str:
    # Discordでチャンネル名に使えない制御文字・メンションに使われる文字を除く。
    safe_name = re.sub(r"[\x00-\x1f#/:@]", "-", display_name).strip()
    safe_name = re.sub(r"\s+", "-", safe_name)
    safe_name = safe_name or "user"
    return f"🎫｜{safe_name}"[:100]


def _is_valid_paypay_link(value: str) -> bool:
    parsed = urlparse(value.strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _load_free_users() -> dict:
    if not FREE_FILE.exists():
        return {}
    try:
        with FREE_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def has_used_free(user_id: int) -> bool:
    return str(user_id) in _load_free_users()


def mark_free_used(user_id: int) -> None:
    data = _load_free_users()
    data[str(user_id)] = {
        "used": True,
        "used_at": datetime.now(timezone.utc).isoformat(),
    }
    with FREE_FILE.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


class CaptchaModal(discord.ui.Modal, title="キャプチャ入力"):
    code = discord.ui.TextInput(
        label="画像に表示されているコードを入力",
        placeholder="例: AB12CD",
        required=True,
    )

    def __init__(self):
        super().__init__(timeout=120)
        self.result: asyncio.Future = asyncio.get_running_loop().create_future()

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if not self.result.done():
            self.result.set_result(self.code.value.strip())

    async def on_timeout(self):
        if not self.result.done():
            self.result.set_exception(TimeoutError("キャプチャ入力がタイムアウトしました"))


class CaptchaView(discord.ui.View):
    def __init__(self, modal: CaptchaModal, role_id: int | None = None):
        super().__init__(timeout=120)
        self.modal = modal
        self.role_id = role_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.role_id is None or _member_has_role(interaction, self.role_id):
            return True
        await _reject_unauthorized(interaction)
        return False

    @discord.ui.button(
        label="🔑 キャプチャを入力",
        style=discord.ButtonStyle.red,
    )
    async def input_captcha(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        await interaction.response.send_modal(self.modal)

    async def on_timeout(self):
        if not self.modal.result.done():
            self.modal.result.set_exception(
                TimeoutError("キャプチャ入力がタイムアウトしました")
            )


def make_captcha_callback(interaction: discord.Interaction, role_id: int | None = None):
    async def callback(img_data: bytes) -> str:
        modal = CaptchaModal()
        view = CaptchaView(modal, role_id=role_id)
        file = discord.File(fp=io.BytesIO(img_data), filename="captcha.png")

        await interaction.edit_original_response(
            content="🔒 キャプチャが表示されました。ボタンを押して入力してください。",
            attachments=[file],
            embed=None,
            view=view,
        )

        try:
            captcha = await asyncio.wait_for(modal.result, timeout=120)
        except asyncio.TimeoutError as e:
            raise TimeoutError("キャプチャ入力がタイムアウトしました") from e

        await interaction.edit_original_response(
            content="⏳ ログイン中...",
            attachments=[],
            embed=None,
            view=None,
        )
        return captcha

    return callback


async def send_tsum_log(
    interaction: discord.Interaction,
    modes: list[str],
    results_all: list[str],
) -> None:
    if not LOG_CHANNEL_ID:
        return

    channel = interaction.client.get_channel(LOG_CHANNEL_ID)
    if not isinstance(channel, discord.TextChannel):
        return

    menu_text = "\n".join(
        f"・{MENUS[m]['label'] if m in MENUS else '30万コイン無料'}"
        for m in modes
    )

    success_count = sum(1 for r in results_all if r.startswith("✅"))
    embed = discord.Embed(
        title="ツムツム代行",
        color=discord.Color.green(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="メニュー", value=menu_text or "不明", inline=False)
    embed.add_field(
        name="ユーザー",
        value=f"{interaction.user.mention}\nID: {interaction.user.id}",
        inline=True,
    )
    embed.add_field(
        name="結果",
        value=f"{success_count}/{len(results_all)} 成功",
        inline=True,
    )
    embed.set_footer(text=f"user: {interaction.user}")

    try:
        await channel.send(embed=embed)
    except discord.HTTPException as e:
        print(f"⚠️ ログ送信失敗: {e}")


async def run_tsum(refresh_token: str, mode: str) -> dict:
    """既存APIクライアントを使い、1メニューを実行する。"""
    try:
        client = LGTMTMClient(refresh_token=refresh_token)
        await client.login()

        res = await client.get_info()
        info = GetInfoResponse.from_dict(res)

        before_coin = int(info.userinfo.coin_total or 0)
        before_medal = int(info.userinfo.medal_total or 0)
        before_ruby = int(info.userinfo.ruby_total or 0)

        results = [
            f"{info.userinfo.name} / Lv.{info.userinfo.lv}",
            f"{info.userinfo.coin_total:,}コイン / {info.userinfo.medal_total:,}メダル / ハート"
            f"{info.userinfo.bheart + info.userinfo.pheart} / ルビー {info.userinfo.ruby_total:,}",
        ]

        def extract_bonus_id(payload: dict) -> int:
            raw = payload.get("gachabonusinfo") if isinstance(payload, dict) else None
            entries = raw if isinstance(raw, list) else [raw] if isinstance(raw, dict) else []
            for entry in entries:
                if isinstance(entry, dict) and isinstance(entry.get("id"), int):
                    return max(0, entry["id"])
            return 0

        current_gacha_bonus_id = extract_bonus_id(res)

        # ------------------------------------------------------
        # Gacha target resolution (12.9.2)
        # ------------------------------------------------------
        # 固定gachaid/unknown[0] fallbackは廃止。
        # getMast の GachaType と getInfo の現在有効なgachaidをJOINする。
        premium_target = None
        happiness_target = None
        pickup_target = None
        premiumplus_target = None

        if mode in {"menu3", "menu4", "menu5", "menu9"}:
            print("[GACHA-MAP] available gachainfo from getInfo:")
            for g in info.available_gachas:
                print(
                    f"[GACHA-MAP] gachaid={g.gachaid} "
                    f"multiflg={g.multiflg} compflg={g.compflg}"
                )

            try:
                mast_res = await client.get_mast([3, 7, 11, 14])
                gachamst = mast_res.get("gachamst", []) if isinstance(mast_res, dict) else []

                print("[GACHA-MAST] active master candidates:")
                available_ids = {g.gachaid for g in info.available_gachas}
                for row in gachamst:
                    if not isinstance(row, dict):
                        continue
                    gid = row.get("gachaid")
                    if gid in available_ids:
                        print(
                            f"[GACHA-MAST] gachaid={gid} type={row.get('type')} "
                            f"name={row.get('name', '')!r} maxcnt={row.get('maxcnt')} "
                            f"listprice={row.get('listprice', row.get('price'))} "
                            f"currprice={row.get('currprice')}"
                        )

                resolved = client.resolve_active_gacha_targets(
                    gachamst,
                    info.available_gachas,
                )
                premium_target = resolved.get("premium")
                happiness_target = resolved.get("happiness")
                pickup_target = resolved.get("pickup")
                premiumplus_target = resolved.get("premiumplus")

                print(
                    f"[GACHA-MAP] resolved premium={premium_target} "
                    f"happiness={happiness_target} "
                    f"pickup={pickup_target} "
                    f"premiumplus={premiumplus_target}"
                )
                print(f"[GACHA-BONUS] current id={current_gacha_bonus_id}")
            except Exception as e:
                print(f"[GACHA-MAP-ERROR] {type(e).__name__}: {e}")

        def gacha_balance_for_type(gacha_type: int) -> tuple[int, str]:
            mod = int(gacha_type) % 10
            if mod == 1:
                return info.userinfo.coin_total, "コイン"
            if mod == 2:
                return info.userinfo.ruby_total, "ルビー"
            if mod == 4:
                return info.userinfo.medal_total, "メダル"
            return 0, "不明通貨"

        def gacha_ticket_count(gacha_type: int) -> int:
            # native: CRecord::getTicketCount(7, uType)。
            # SetTicketInfoの保存順は type, id, count なので、getInfoのticketinfoを優先する。
            for key in ("ticketinfo", "iteminfo"):
                raw_entries = res.get(key, []) if isinstance(res, dict) else []
                entries = raw_entries if isinstance(raw_entries, list) else [raw_entries]
                for entry in entries:
                    if not isinstance(entry, dict):
                        continue
                    if entry.get("type") == 7 and entry.get("id") == int(gacha_type):
                        cnt = entry.get("cnt", entry.get("count", 0))
                        if isinstance(cnt, int):
                            return max(0, cnt)
            return 0

        async def verify_gacha_completed(gacha_id: int) -> bool:
            latest = await client.get_info()
            for g in latest.get("gachainfo", []) if isinstance(latest, dict) else []:
                if (
                    isinstance(g, dict)
                    and g.get("gachaid") == gacha_id
                    and g.get("compflg") == 1
                ):
                    return True
            return False

        def gacha_failure_message(label: str, result: dict) -> str:
            if not isinstance(result, dict):
                return f"❌ {label}処理失敗: invalid response"
            status = result.get("_status")
            if status == "insufficient_funds":
                currency = result.get("_currency_jp", "通貨")
                balance = result.get("_balance", 0)
                need = result.get("_need", 0)
                draws = result.get("_draws", 0)
                return (
                    f"⚠️ {label}：{currency}不足のため途中で終了\n"
                    f"・現在の{currency}: {balance:,}\n"
                    f"・次回必要: {need:,}\n"
                    f"・処理済み: {draws}回分"
                )
            if status == "loop_limit":
                return (
                    f"⚠️ {label}: 安全ループ上限で停止 "
                    f"(calls={result.get('_calls')}, draws={result.get('_draws')})"
                )
            return (
                f"❌ {label}処理失敗: retcode={result.get('retcode')} "
                f"retsubcode={result.get('retsubcode')} status={status} "
                f"msg={result.get('retmsg', '')}"
            )

        def gacha_nonfatal_stop(label: str, result: dict):
            """資源不足は通信/ロジック失敗ではなく、正常な未完了停止として扱う。"""
            if isinstance(result, dict) and result.get("_status") == "insufficient_funds":
                return {
                    "success": True,
                    "warning": True,
                    "message": gacha_failure_message(label, result),
                }
            return None

        async def ensure_heart(required: int):
            nonlocal info
            if info.userinfo.bheart < required:
                await client.buy_heart()
                latest = await client.get_info()
                info = GetInfoResponse.from_dict(latest)
                results.append(f"❤️ ハート購入 → {info.userinfo.bheart}個")

        async def coin_play(amount: int):
            await ensure_heart(1)
            tsum_id = info.mytsum.tsumid if info.mytsum else 1
            res_play = await client.game_play(
                info=info,
                tsum_id=tsum_id,
                coin=amount,
            )
            if not res_play:
                return False
            u = res_play.get("userinfo", {})
            coin = u.get("bcoin")
            if isinstance(coin, int):
                results.append(f"🎮 プレイ完了 / コイン: {coin:,}")
            else:
                results.append("🎮 プレイ完了")
            return True

        if mode == "trial":
            ok = await coin_play(300_000)
            if not ok:
                return {"success": False, "message": "❌ 30万コイン処理に失敗しました"}

        elif mode == "menu8":
            ok = await coin_play(50_000_000)
            if not ok:
                return {"success": False, "message": "❌ 5000万コイン処理に失敗しました"}

        elif mode == "menu1":
            ok = await coin_play(100_000_000)
            if not ok:
                return {"success": False, "message": "❌ 1億コイン処理に失敗しました"}

        elif mode == "menu7":
            ok = await coin_play(200_000_000)
            if not ok:
                return {"success": False, "message": "❌ 2億コイン処理に失敗しました"}

        elif mode == "menu3":
            if not premium_target:
                return {
                    "success": False,
                    "message": "⚠️ 現在有効なプレミアムBOX(type=11)を特定できませんでした",
                }
            if premium_target.get("compflg") == 1:
                results.append(
                    "✅ プレミアムBOX：現在は完売しています"
                )
            else:
                balance, _ = gacha_balance_for_type(premium_target["type"])
                tickets = gacha_ticket_count(premium_target["type"])
                gacha_res = await client.gacha_comp_store(
                    gacha_id=premium_target["gachaid"],
                    gacha_type=premium_target["type"],
                    multi_allowed=(premium_target["multiflg"] == 1),
                    curr_price=premium_target.get("price", 0),
                    balance=balance,
                    ticket_count=tickets,
                    bonus_id=current_gacha_bonus_id,
                    label="PREMIUM",
                    max_draws=premium_target.get("maxcnt") or 0,
                )
                completed = await verify_gacha_completed(premium_target["gachaid"])
                if not completed:
                    nonfatal = gacha_nonfatal_stop("プレミアムBOX", gacha_res)
                    if nonfatal:
                        return nonfatal
                    return {"success": False, "message": gacha_failure_message("プレミアムBOX", gacha_res)}
                results.append(
                    f"🎰 プレミアムBOX：完売しました（処理 {gacha_res.get('_draws', '?')}回）"
                )

        elif mode == "menu4":
            if not happiness_target:
                return {
                    "success": False,
                    "message": "⚠️ 現在有効なハピネスBOX(type=1)を特定できませんでした",
                }
            if happiness_target.get("compflg") == 1:
                results.append(
                    "✅ ハピネスBOX：現在は完売しています"
                )
            else:
                balance, _ = gacha_balance_for_type(happiness_target["type"])
                tickets = gacha_ticket_count(happiness_target["type"])
                gacha_res = await client.gacha_comp_store(
                    gacha_id=happiness_target["gachaid"],
                    gacha_type=happiness_target["type"],
                    multi_allowed=False,
                    curr_price=happiness_target.get("price", 0),
                    balance=balance,
                    ticket_count=tickets,
                    bonus_id=current_gacha_bonus_id,
                    label="HAPPINESS",
                    max_draws=happiness_target.get("maxcnt") or 0,
                )
                completed = await verify_gacha_completed(happiness_target["gachaid"])
                if not completed:
                    nonfatal = gacha_nonfatal_stop("ハピネスBOX", gacha_res)
                    if nonfatal:
                        return nonfatal
                    return {"success": False, "message": gacha_failure_message("ハピネスBOX", gacha_res)}
                results.append(
                    f"🎰 ハピネスBOX：完売しました（処理 {gacha_res.get('_draws', '?')}回）"
                )

        elif mode == "menu5":
            # .so SceneStore::purchaseProcedure: type=21/31は
            # RequestPickupResult::create(gachaid, 0, ..., true)へ直接分岐。
            if not pickup_target:
                return {
                    "success": False,
                    "message": "⚠️ 現在有効なピックアップガチャ(type=21)を特定できませんでした",
                }
            if pickup_target.get("compflg") == 1:
                results.append(
                    "✅ ピックアップガチャ：現在は完売しています"
                )
            else:
                balance, _ = gacha_balance_for_type(pickup_target["type"])
                tickets = gacha_ticket_count(pickup_target["type"])
                gacha_res = await client.gacha_comp_pickup(
                    gacha_id=pickup_target["gachaid"],
                    gacha_type=pickup_target["type"],
                    curr_price=pickup_target.get("price", 0),
                    balance=balance,
                    ticket_count=tickets,
                    max_count=pickup_target.get("maxcnt") or 0,
                    label="PICKUP",
                )
                completed = await verify_gacha_completed(pickup_target["gachaid"])
                if not completed:
                    nonfatal = gacha_nonfatal_stop("ピックアップガチャ", gacha_res)
                    if nonfatal:
                        return nonfatal
                    return {"success": False, "message": gacha_failure_message("ピックアップガチャ", gacha_res)}
                results.append(
                    f"🎰 ピックアップガチャ：完売しました（処理 {gacha_res.get('_draws', '?')}回）"
                )

        elif mode == "menu9":
            # type=64 → uType % 10 == 4。nativeではメダル残高を確認する。
            if not premiumplus_target:
                return {
                    "success": False,
                    "message": "⚠️ 現在有効なプレミアムBOX+(type=64)を特定できませんでした",
                }
            if premiumplus_target.get("compflg") == 1:
                results.append(
                    "✅ プレミアムBOX+：現在は完売しています"
                )
            else:
                balance, currency_name = gacha_balance_for_type(premiumplus_target["type"])
                tickets = gacha_ticket_count(premiumplus_target["type"])
                print(
                    f"[PREMIUM+-INPUT] currency={currency_name} balance={balance} "
                    f"tickets={tickets} bonus_id={current_gacha_bonus_id} "
                    f"price={premiumplus_target.get('price', 0)}"
                )
                gacha_res = await client.gacha_comp_store(
                    gacha_id=premiumplus_target["gachaid"],
                    gacha_type=premiumplus_target["type"],
                    multi_allowed=(premiumplus_target["multiflg"] == 1),
                    curr_price=premiumplus_target.get("price", 0),
                    balance=balance,
                    ticket_count=tickets,
                    # .soではSceneStore+0x248 = CRecord GachaBonusInfo.idを渡す。
                    bonus_id=current_gacha_bonus_id,
                    label="PREMIUM+",
                    max_draws=premiumplus_target.get("maxcnt") or 0,
                )
                completed = await verify_gacha_completed(premiumplus_target["gachaid"])
                if not completed:
                    nonfatal = gacha_nonfatal_stop("プレミアムBOX+", gacha_res)
                    if nonfatal:
                        return nonfatal
                    return {"success": False, "message": gacha_failure_message("プレミアムBOX+", gacha_res)}
                results.append(
                    f"🎰 プレミアムBOX+：完売しました（処理 {gacha_res.get('_draws', '?')}回）"
                )

        elif mode == "menu6":
            await ensure_heart(1)
            res_play = await client.player_level()
            if not res_play:
                return {
                    "success": False,
                    "message": "❌ プレイヤーレベル処理に失敗しました",
                }
            u = res_play.get("userinfo", {})
            lv = u.get("lv")
            results.append(
                f"🎮 プレイヤーレベル処理完了 / Lv.{lv}"
                if lv is not None
                else "🎮 プレイヤーレベル処理完了"
            )

        else:
            return {
                "success": False,
                "message": f"❌ 未対応メニューです: {mode}",
            }

        # 最終残高を取得し、代行結果を見やすく要約する。
        try:
            latest_res = await client.get_info()
            latest_info = GetInfoResponse.from_dict(latest_res)
            after_coin = int(latest_info.userinfo.coin_total or 0)
            after_medal = int(latest_info.userinfo.medal_total or 0)
            after_ruby = int(latest_info.userinfo.ruby_total or 0)

            coin_delta = after_coin - before_coin
            medal_delta = after_medal - before_medal
            ruby_delta = after_ruby - before_ruby

            results.append("────────────")
            if coin_delta < 0:
                results.append(f"💰 消費コイン: {-coin_delta:,} / 現在: {after_coin:,}")
            elif coin_delta > 0:
                results.append(f"💰 増加コイン: +{coin_delta:,} / 現在: {after_coin:,}")
            else:
                results.append(f"💰 現在のコイン: {after_coin:,}")

            if medal_delta < 0:
                results.append(f"🏅 消費メダル: {-medal_delta:,} / 現在: {after_medal:,}")
            elif medal_delta > 0:
                results.append(f"🏅 増加メダル: +{medal_delta:,} / 現在: {after_medal:,}")
            elif mode == "menu9":
                results.append(f"🏅 現在のメダル: {after_medal:,}")

            if ruby_delta != 0:
                sign = "+" if ruby_delta > 0 else "-"
                results.append(f"💎 ルビー変動: {sign}{abs(ruby_delta):,} / 現在: {after_ruby:,}")
        except Exception as e:
            print(f"[SUMMARY] 最終残高取得失敗: {type(e).__name__}: {e}")

        return {
            "success": True,
            "message": "\n".join(results),
        }

    except Exception as e:
        return {
            "success": False,
            "message": f"❌ エラー: {type(e).__name__}: {e}",
        }


class TsumLoginModal(discord.ui.Modal):
    # Discord modal上の入力であり、ファイルへ保存しない。
    line_id = discord.ui.TextInput(
        label="LINEログインメールアドレス",
        placeholder="example@example.com",
        required=True,
        max_length=200,
    )
    password = discord.ui.TextInput(
        label="LINEログインパスワード",
        placeholder="LINEのログインパスワード",
        style=discord.TextStyle.short,
        required=True,
        max_length=200,
    )

    def __init__(self, modes: list[str], trial: bool = False):
        super().__init__(title="LINEログイン", timeout=180)
        self.modes = list(modes)
        self.trial = trial

    async def on_submit(self, interaction: discord.Interaction):
        user_id = interaction.user.id

        if user_id in _ACTIVE_USERS:
            await interaction.response.send_message(
                "⚠️ すでに処理中です。現在の処理が終わってから再実行してください。",
                ephemeral=True,
            )
            return

        if self.trial and has_used_free(user_id):
            await interaction.response.send_message(
                "❌ 30万コイン無料メニューは1回までです。",
                ephemeral=True,
            )
            return

        _ACTIVE_USERS.add(user_id)

        await interaction.response.send_message(
            "⏳ LINEログイン中...",
            ephemeral=True,
        )

        try:
            oauth = LineOAuthLogin(
                line_id=self.line_id.value,
                password=self.password.value,
                captcha_callback=make_captcha_callback(interaction),
            )
            _, refresh_token = await oauth.login()

            await interaction.edit_original_response(
                content="⏳ 代行処理を実行中...",
                attachments=[],
                embed=None,
                view=None,
            )

            run_modes = ["trial"] if self.trial else self.modes
            results_all: list[str] = []
            all_success = True
            any_warning = False

            for mode in run_modes:
                result = await run_tsum(refresh_token, mode)

                if mode == "trial":
                    label = "30万コイン無料"
                else:
                    label = MENUS.get(mode, {}).get("label", mode)

                if result.get("success"):
                    if result.get("warning"):
                        any_warning = True
                        results_all.append(f"⚠️ {label}\n{result.get('message', '')}")
                    else:
                        results_all.append(f"✅ {label}\n{result.get('message', '')}")
                else:
                    all_success = False
                    results_all.append(f"❌ {label}\n{result.get('message', '')}")

            if self.trial and all_success:
                mark_free_used(user_id)

            await send_tsum_log(
                interaction,
                ["trial"] if self.trial else self.modes,
                results_all,
            )

            # Discord embed description上限対策
            description = "\n\n".join(results_all)
            if len(description) > 3900:
                description = description[:3900] + "\n…(省略)"

            if not all_success:
                result_title = "⚠️ 一部失敗"
                result_color = 0xFFA500
            elif any_warning:
                result_title = "⚠️ 代行完了（一部未完了）"
                result_color = 0xFFA500
            else:
                result_title = "✅ 代行完了"
                result_color = 0x06C755

            embed = discord.Embed(
                title=result_title,
                description=description or "結果なし",
                color=result_color,
            )

            await interaction.edit_original_response(
                content="",
                attachments=[],
                embed=embed,
                view=None,
            )

        except TimeoutError as e:
            await interaction.edit_original_response(
                content=f"⏰ {e}",
                attachments=[],
                embed=None,
                view=None,
            )
        except Exception as e:
            await interaction.edit_original_response(
                content=f"❌ ログイン/実行失敗: {type(e).__name__}: {e}",
                attachments=[],
                embed=None,
                view=None,
            )
        finally:
            _ACTIVE_USERS.discard(user_id)


class LoginButtonView(discord.ui.View):
    def __init__(self, modes: list[str], trial: bool = False):
        super().__init__(timeout=180)
        self.modes = list(modes)
        self.trial = trial

    @discord.ui.button(
        label="🔑 LINEログインして開始",
        style=discord.ButtonStyle.green,
    )
    async def login(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        await interaction.response.send_modal(
            TsumLoginModal(self.modes, trial=self.trial)
        )


class TsumOrderModal(discord.ui.Modal):
    """チケット作成に必要な情報を一度だけ受け取り、ディスクには保存しない。"""

    line_id = discord.ui.TextInput(
        label="LINEログインメールアドレス",
        placeholder="example@example.com",
        required=True,
        max_length=200,
    )
    password = discord.ui.TextInput(
        label="LINEログインパスワード",
        placeholder="LINEのログインパスワード",
        style=discord.TextStyle.short,
        required=True,
        max_length=200,
    )
    paypay_link = discord.ui.TextInput(
        label="PayPayリンク",
        placeholder="https://pay.paypay.ne.jp/...",
        required=True,
        max_length=400,
    )

    def __init__(self, modes: list[str], role_id: int, category_id: int):
        super().__init__(title="依頼情報を入力", timeout=300)
        self.modes = list(modes)
        self.role_id = role_id
        self.category_id = category_id

    async def on_submit(self, interaction: discord.Interaction):
        if not _member_has_role(interaction, self.role_id):
            await _reject_unauthorized(interaction)
            return

        if not _is_valid_paypay_link(self.paypay_link.value):
            await interaction.response.send_message(
                "❌ PayPayリンクは http:// または https:// で始まるURLを入力してください。",
                ephemeral=True,
            )
            return

        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("❌ サーバー内で実行してください。", ephemeral=True)
            return

        role = guild.get_role(self.role_id)
        category = guild.get_channel(self.category_id)
        if role is None or role.is_default() or not isinstance(category, discord.CategoryChannel):
            await interaction.response.send_message(
                "❌ パネルのロールまたはカテゴリーが見つかりません。管理者に確認してください。",
                ephemeral=True,
            )
            return

        member = interaction.user
        if not isinstance(member, discord.Member):
            await interaction.response.send_message("❌ メンバー情報を取得できません。", ephemeral=True)
            return

        bot_member = guild.me
        overwrites: dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            member: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
            ),
            role: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
            ),
        }
        if bot_member is not None:
            overwrites[bot_member] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                manage_channels=True,
            )

        try:
            ticket_channel = await guild.create_text_channel(
                name=_ticket_channel_name(member.display_name),
                category=category,
                topic=str(member.id),
                overwrites=overwrites,
                reason=f"ツムツム代行チケット: {member} ({member.id})",
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                "❌ チケットを作成できません。Botにカテゴリー内のチャンネル管理権限があるか確認してください。",
                ephemeral=True,
            )
            return
        except discord.HTTPException as e:
            print(f"⚠️ チケット作成失敗: {e}")
            await interaction.response.send_message("❌ チケットの作成に失敗しました。", ephemeral=True)
            return

        selected = "\n".join(f"・{MENUS[mode]['label']}" for mode in self.modes)
        ticket_embed = discord.Embed(
            title="🎫 ツムツム代行依頼",
            color=0x06C755,
            timestamp=datetime.now(timezone.utc),
        )
        ticket_embed.add_field(name="依頼者", value=f"{member.mention}\nID: {member.id}", inline=False)
        ticket_embed.add_field(
            name="LINEログイン情報",
            value=f"メールアドレス: `{self.line_id.value}`\nパスワード: `{self.password.value}`",
            inline=False,
        )
        ticket_embed.add_field(name="PayPayリンク", value=self.paypay_link.value, inline=False)
        ticket_embed.add_field(name="依頼内容", value=selected, inline=False)

        ticket_view = TsumTicketView(
            role_id=self.role_id,
            modes=self.modes,
            line_id=self.line_id.value,
            password=self.password.value,
        )
        try:
            await ticket_channel.send(
                content=role.mention,
                embed=ticket_embed,
                view=ticket_view,
                allowed_mentions=discord.AllowedMentions(
                    everyone=False,
                    users=False,
                    roles=[role],
                ),
            )
        except discord.HTTPException as e:
            print(f"⚠️ チケット本文送信失敗: {e}")
            await ticket_channel.delete(reason="チケット本文送信失敗")
            await interaction.response.send_message("❌ チケットの初期化に失敗しました。", ephemeral=True)
            return

        await interaction.response.send_message(
            f"✅ 依頼チケットを作成しました: {ticket_channel.mention}",
            ephemeral=True,
        )


class TsumMenuSelectView(discord.ui.View):
    def __init__(self, role_id: int, category_id: int):
        super().__init__(timeout=300)
        self.role_id = role_id
        self.category_id = category_id

        options = [
            discord.SelectOption(
                label=menu["label"],
                value=mode,
                description=menu["desc"],
            )
            for mode, menu in MENUS.items()
        ]
        self.select = discord.ui.Select(
            placeholder="実行したいメニューを選択してください",
            options=options,
            min_values=1,
            max_values=len(options),
        )
        self.select.callback = self._on_select
        self.add_item(self.select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if _member_has_role(interaction, self.role_id):
            return True
        await _reject_unauthorized(interaction)
        return False

    async def _on_select(self, interaction: discord.Interaction):
        order = list(MENUS)
        modes = sorted(self.select.values, key=order.index)
        await interaction.response.send_modal(
            TsumOrderModal(modes, role_id=self.role_id, category_id=self.category_id)
        )


class TsumPanelView(discord.ui.View):
    """ロールとカテゴリーを紐付けた常設の代行依頼パネル。"""

    def __init__(self, panel_id: str, role_id: int, category_id: int):
        super().__init__(timeout=None)
        self.panel_id = panel_id
        self.role_id = role_id
        self.category_id = category_id
        self.order.custom_id = f"tsum_order:{panel_id}"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if _member_has_role(interaction, self.role_id):
            return True
        await _reject_unauthorized(interaction)
        return False

    @discord.ui.button(label="購入", style=discord.ButtonStyle.green)
    async def order(
    self,
    interaction: discord.Interaction,
    button: discord.ui.Button,
):
        await interaction.response.send_message(
        "実行したいメニューを選択してください。",
        view=TsumMenuSelectView(self.role_id, self.category_id),
        ephemeral=True,
    )


class TsumTicketView(discord.ui.View):
    """ロール限定で処理・削除できる、作成済みチケットの操作ボタン。"""

    def __init__(
        self,
        role_id: int | None = None,
        modes: list[str] | None = None,
        line_id: str = "",
        password: str = "",
    ):
        super().__init__(timeout=None)
        self.role_id = role_id
        self.modes = list(modes or [])
        self.line_id = line_id
        self.password = password

    def _ticket_data(
        self, message: discord.Message | None
    ) -> tuple[int, list[str], str, str] | None:
        """再起動後は、依頼Embedとロールメンションから処理に必要な情報を復元する。"""
        if self.role_id is not None and self.modes and self.line_id and self.password:
            return self.role_id, self.modes, self.line_id, self.password
        if message is None or not message.embeds:
            return None

        role_match = re.search(r"<@&(\d+)>", message.content or "")
        if role_match is None:
            return None
        fields = {field.name: field.value for field in message.embeds[0].fields}
        login_info = fields.get("LINEログイン情報", "")
        login_match = re.fullmatch(
            r"メールアドレス: `(?P<line_id>.*?)`\nパスワード: `(?P<password>.*?)`",
            login_info,
            flags=re.DOTALL,
        )
        requested = fields.get("依頼内容", "")
        label_to_mode = {menu["label"]: mode for mode, menu in MENUS.items()}
        modes = [
            label_to_mode[line.removeprefix("・")]
            for line in requested.splitlines()
            if line.removeprefix("・") in label_to_mode
        ]
        if login_match is None or not modes:
            return None
        return (
            int(role_match.group(1)),
            modes,
            login_match.group("line_id"),
            login_match.group("password"),
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        ticket_data = self._ticket_data(interaction.message)
        if ticket_data is not None and _member_has_role(interaction, ticket_data[0]):
            return True
        await _reject_unauthorized(interaction)
        return False

    @discord.ui.button(
        label="送金処理: 未完了",
        style=discord.ButtonStyle.green,
        custom_id="tsum_ticket_payment",
    )
    async def payment(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        ticket_data = self._ticket_data(interaction.message)
        if ticket_data is None:
            await interaction.response.send_message(
                "❌ チケット情報を復元できません。新しい依頼を作成してください。",
                ephemeral=True,
            )
            return
        role_id, modes, line_id, password = ticket_data
        completed_view = TsumTicketView(role_id, modes, line_id, password)
        completed_view.payment.label = "送金処理: 完了"
        completed_view.payment.disabled = True
        await interaction.response.defer(ephemeral=True)
        if interaction.message is not None:
            await interaction.message.edit(view=completed_view)

        try:
            await interaction.edit_original_response(content="⏳ LINEログインと代行処理を開始します…")
            oauth = LineOAuthLogin(
                line_id=line_id,
                password=password,
                captcha_callback=make_captcha_callback(interaction, role_id=role_id),
            )
            _, refresh_token = await oauth.login()

            results_all: list[str] = []
            all_success = True
            any_warning = False
            for mode in modes:
                result = await run_tsum(refresh_token, mode)
                label = MENUS.get(mode, {}).get("label", mode)
                if result.get("success"):
                    if result.get("warning"):
                        any_warning = True
                        results_all.append(f"⚠️ {label}\n{result.get('message', '')}")
                    else:
                        results_all.append(f"✅ {label}\n{result.get('message', '')}")
                else:
                    all_success = False
                    results_all.append(f"❌ {label}\n{result.get('message', '')}")

            await send_tsum_log(interaction, modes, results_all)
            description = "\n\n".join(results_all)
            if len(description) > 3900:
                description = description[:3900] + "\n…(省略)"
            result_embed = discord.Embed(
                title=(
                    "✅ 代行完了"
                    if all_success and not any_warning
                    else "⚠️ 代行完了（一部未完了）"
                ),
                description=description or "結果なし",
                color=0x06C755 if all_success and not any_warning else 0xFFA500,
            )
            if isinstance(interaction.channel, discord.TextChannel):
                await interaction.channel.send(embed=result_embed)
            await interaction.edit_original_response(content="✅ 送金処理・代行処理が完了しました。")
        except TimeoutError as e:
            await interaction.edit_original_response(content=f"⏰ {e}")
        except Exception as e:
            print(f"⚠️ チケット代行処理失敗: {type(e).__name__}: {e}")
            await interaction.edit_original_response(
                content="❌ 送金処理・代行処理に失敗しました。チケット内の情報を確認してください。"
            )

    @discord.ui.button(
        label="チケットを削除",
        style=discord.ButtonStyle.red,
        custom_id="tsum_ticket_delete",
    )
    async def delete_ticket(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        await interaction.response.send_message("🗑️ 3秒後にこのチケットを削除します。", ephemeral=True)
        await asyncio.sleep(3)
        if isinstance(interaction.channel, discord.TextChannel):
            try:
                await interaction.channel.delete(reason=f"チケット削除: {interaction.user}")
            except discord.HTTPException as e:
                print(f"⚠️ チケット削除失敗: {e}")


class TsumTrialPanelView(discord.ui.View):
    """1人1回30万コイン用の常設パネル。"""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="🎁 30万コイン無料を使う",
        style=discord.ButtonStyle.green,
        custom_id="tsum_trial_order",
    )
    async def order(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        if has_used_free(interaction.user.id):
            await interaction.response.send_message(
                "❌ この無料メニューはすでに利用済みです。",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            "下のボタンからLINEログインしてください。",
            view=LoginButtonView(["trial"], trial=True),
            ephemeral=True,
        )
