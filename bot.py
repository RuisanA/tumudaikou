import os
import traceback
from pathlib import Path

import discord
from discord import app_commands
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"

load_dotenv(ENV_FILE, override=True)

TOKEN = os.getenv("TOKEN", "").strip()
OWNER_ID_RAW = os.getenv("OWNER_ID", "0").strip()
SERVER_ID_RAW = os.getenv("SERVER_ID", "0").strip()
LOG_CHANNEL_ID_RAW = os.getenv("LOG_CHANNEL_ID", "0").strip()

if not TOKEN:
    raise RuntimeError(f"TOKENが設定されていません: {ENV_FILE}")

try:
    OWNER_ID = int(OWNER_ID_RAW)
except ValueError as e:
    raise RuntimeError(f"OWNER_IDが正しい数字ではありません: {OWNER_ID_RAW}") from e

try:
    SERVER_ID = int(SERVER_ID_RAW)
except ValueError as e:
    raise RuntimeError(f"SERVER_IDが正しい数字ではありません: {SERVER_ID_RAW}") from e

try:
    LOG_CHANNEL_ID = int(LOG_CHANNEL_ID_RAW)
except ValueError as e:
    raise RuntimeError(f"LOG_CHANNEL_IDが正しい数字ではありません: {LOG_CHANNEL_ID_RAW}") from e

# mod_stum は dotenv 読み込み後に import
import mod_stum


class TsumBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        # persistent view
        for config in mod_stum.load_tsum_panel_configs():
            self.add_view(
                mod_stum.TsumPanelView(
                    panel_id=str(config["panel_id"]),
                    role_id=int(config["role_id"]),
                    category_id=int(config["category_id"]),
                )
            )
        # チケット操作は依頼Embedから復元できるため、再起動後も受け付ける。
        self.add_view(mod_stum.TsumTicketView())
        self.add_view(mod_stum.TsumTrialPanelView())

        if SERVER_ID:
            guild = discord.Object(id=SERVER_ID)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            print(f"✅ SERVER Commands synced ({len(synced)} commands)")
            print(f"🖥️ SERVER_ID: {SERVER_ID}")
        else:
            synced = await self.tree.sync()
            print(f"✅ Global Commands synced ({len(synced)} commands)")

        for command in synced:
            print(f"   /{command.name}")

    async def on_ready(self):
        print()
        print("=" * 60)
        print("🤖 Tsum Bot Is Ready.")
        print(f"Bot: {self.user}")
        print(f"Bot ID: {self.user.id if self.user else 'unknown'}")
        print(f"OWNER_ID: {OWNER_ID}")
        print(f"SERVER_ID: {SERVER_ID}")
        print(f"LOG_CHANNEL_ID: {LOG_CHANNEL_ID}")
        print("参加サーバー:")
        for guild in self.guilds:
            mark = " ← TARGET" if SERVER_ID and guild.id == SERVER_ID else ""
            print(f"  {guild.name} / {guild.id}{mark}")
        print("=" * 60)
        print()


bot = TsumBot()


@bot.tree.command(name="ツムツム代行", description="ツムツム代行のチケット受付パネルを表示します")
@app_commands.describe(
    role="パネルとチケットのボタン操作を許可するロール",
    category="依頼チケットを作成するカテゴリー",
)
async def tsum_panel(
    interaction: discord.Interaction,
    role: discord.Role,
    category: discord.CategoryChannel,
):
    if interaction.guild is None:
        await interaction.response.send_message("❌ サーバー内で実行してください。", ephemeral=True)
        return
    if role.is_default():
        await interaction.response.send_message(
            "❌ `@everyone` ではなく、操作を許可する専用ロールを指定してください。",
            ephemeral=True,
        )
        return

    menu_text = "\n".join(
        f"・**{menu['label']}** — {menu['desc']}"
        for menu in mod_stum.MENUS.values()
    )

    embed = discord.Embed(
        title="ツムツム自動代行",
        description=(
            f"{menu_text}\n"
            f"※ 最短1分"
            f"メールアドレスとパスワードに誤りがないようにしてください"
            f"※ ログイン情報はファイルへ保存しません。"
        ),
        color=0x06C755,
    )

    panel_id = mod_stum.new_panel_id()
    await interaction.response.send_message(
        embed=embed,
        view=mod_stum.TsumPanelView(panel_id, role.id, category.id),
    )
    try:
        mod_stum.save_tsum_panel_config(panel_id, role.id, category.id)
    except OSError as e:
        print(f"⚠️ パネル設定保存失敗: {e}")
        await interaction.followup.send(
            "⚠️ パネルは表示しましたが、再起動後に復元できない可能性があります。",
            ephemeral=True,
        )


@bot.tree.command(name="ツムツム無料30万", description="1人1回の30万コイン無料メニューを表示します")
async def tsum_trial_panel(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🎁 30万コイン無料メニュー",
        description=(
            "1人1回まで利用できます。\n"
            "ボタンを押してLINEログイン後、自動で30万コイン処理を実行します。"
        ),
        color=0x06C755,
    )
    await interaction.response.send_message(
        embed=embed,
        view=mod_stum.TsumTrialPanelView(),
    )


@bot.tree.command(name="ツムツム状態", description="Bot設定と登録コマンドの状態を確認します")
async def tsum_status(interaction: discord.Interaction):
    if OWNER_ID and interaction.user.id != OWNER_ID:
        await interaction.response.send_message("❌ OWNER専用です。", ephemeral=True)
        return

    target_name = "未参加"
    if SERVER_ID:
        guild = bot.get_guild(SERVER_ID)
        if guild:
            target_name = guild.name

    text = (
        f"Bot: `{bot.user}`\n"
        f"Bot ID: `{bot.user.id if bot.user else 0}`\n"
        f"SERVER_ID: `{SERVER_ID}` ({target_name})\n"
        f"LOG_CHANNEL_ID: `{LOG_CHANNEL_ID}`\n"
        f"メニュー数: `{len(mod_stum.MENUS)}`"
    )
    await interaction.response.send_message(text, ephemeral=True)


@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
):
    print(f"❌ Slash command error: {type(error).__name__}: {error}")
    traceback.print_exception(type(error), error, error.__traceback__)
    if not interaction.response.is_done():
        await interaction.response.send_message(
            "❌ コマンド実行中にエラーが発生しました。コンソールを確認してください。",
            ephemeral=True,
        )


if __name__ == "__main__":
    print("=" * 60)
    print(f"📂 BASE_DIR: {BASE_DIR}")
    print(f"📄 ENV_FILE: {ENV_FILE}")
    print(f"👑 OWNER_ID: {OWNER_ID}")
    print(f"🖥️ SERVER_ID: {SERVER_ID}")
    print("=" * 60)
    bot.run(TOKEN)
