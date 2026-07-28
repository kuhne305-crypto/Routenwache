import discord
from discord.ext import commands, tasks
from discord import app_commands
from datetime import datetime
import pytz
import json
import os

# ════════════════════════════════════════════════════════════════════════════
# ⚙️  KONFIGURATION
# ════════════════════════════════════════════════════════════════════════════
TOKEN    = os.environ.get("DISCORD_TOKEN")
GUILD_ID = os.environ.get("GUILD_ID")
TIMEZONE = pytz.timezone("Europe/Berlin")
EMBED_COLOR = 0xFFD700  # Gelb
DATA_DIR  = "/data" if os.path.isdir("/data") else "."
DATA_FILE = os.path.join(DATA_DIR, "data.json")

# Feste Zeiträume der Routenwache, jeweils max. 3 Plätze.
SLOTS = ["20-21", "21-22", "22-23", "23-24"]
MAX_PLAETZE_PRO_SLOT = 3

# Leitung: darf das Setup (Channel setzen, Nachricht posten) erledigen.
# Ein-/Austragen in einen Zeitraum ist bewusst für ALLE offen.
LEITUNG_ROLLE_ID = 1526202327483285629

def ist_admin_oder_leitung(interaction: discord.Interaction) -> bool:
    """True für echte Admins ODER Mitglieder mit der Leitungs-Rolle.
    Wird nur für die Setup-Befehle benutzt (Channel setzen / Nachricht posten)."""
    if interaction.user.guild_permissions.administrator:
        return True
    return any(r.id == LEITUNG_ROLLE_ID for r in interaction.user.roles)


# ════════════════════════════════════════════════════════════════════════════
# 💾  DATENSPEICHER
# ════════════════════════════════════════════════════════════════════════════
def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            geladen = json.load(f)
    else:
        geladen = {}

    standard = {
        "channel_stempel": 1531376112226140260,
        "stempel_nachricht_id": None,
        "channel_stempel_liste": 1531376274130341909,
        "stempel_liste_nachricht_id": None,
        "tage": {},  # { "28.07.2026": { "20-21": ["userid", ...], "21-22": [...], ... } }
    }
    if not geladen:
        return standard
    for key, wert in standard.items():
        geladen.setdefault(key, wert)
    return geladen

def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)

data = load_data()


# ════════════════════════════════════════════════════════════════════════════
# 🤖  BOT SETUP
# ════════════════════════════════════════════════════════════════════════════
intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot  = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree


# ════════════════════════════════════════════════════════════════════════════
# 🛣️  ROUTENWACHE (Zeitraum-Anmeldung für den heutigen Tag)
# ════════════════════════════════════════════════════════════════════════════
# Komplett offen: es gibt hier absichtlich KEINE Rollen-/Berechtigungs-
# einschränkung. Jedes Mitglied kann sich für einen Zeitraum ein-/austragen
# und auch andere Mitglieder ein-/austragen.

def heute_key() -> str:
    """Aktuelles Datum als String, z.B. '28.07.2026'."""
    return datetime.now(TIMEZONE).strftime("%d.%m.%Y")

def slot_label(slot: str) -> str:
    start, ende = slot.split("-")
    return f"{start} - {ende} Uhr"

def get_tag_eintrag(datum: str) -> dict:
    """Holt (oder erstellt) die Slot-Liste für ein bestimmtes Datum."""
    tage = data.setdefault("tage", {})
    eintrag = tage.setdefault(datum, {})
    for slot in SLOTS:
        eintrag.setdefault(slot, [])
    return eintrag

def finde_slot_von_user(eintrag: dict, uid: str):
    """Gibt den Slot zurück, in dem uid heute bereits eingetragen ist (oder None)."""
    for slot, liste in eintrag.items():
        if uid in liste:
            return slot
    return None

def build_wache_embed(datum: str, guild: discord.Guild) -> discord.Embed:
    embed = discord.Embed(title=f"🛣️ Routenwache Heute ({datum})", color=EMBED_COLOR)
    eintrag = get_tag_eintrag(datum)

    bloecke = []
    for slot in SLOTS:
        leute = eintrag.get(slot, [])
        namen = []
        for uid in leute:
            member = guild.get_member(int(uid)) if guild else None
            namen.append(member.mention if member else f"Unbekanntes Mitglied ({uid})")
        text = "\n".join(namen) if namen else "*– frei –*"
        voll_hinweis = " 🔒 (voll)" if len(leute) >= MAX_PLAETZE_PRO_SLOT else ""
        bloecke.append(f"**{slot_label(slot)}**{voll_hinweis}\n{text}")

    embed.description = "\n\n".join(bloecke)
    embed.set_footer(text="ECLIPSE – Routenwache • Wähle unten deinen Zeitraum (max. 3 Plätze pro Stunde)")
    embed.timestamp = datetime.now(TIMEZONE)
    return embed


class WacheView(discord.ui.View):
    """Persistente View mit einem Button pro Zeitraum. Wird dynamisch
    neu aufgebaut, damit volle Zeiträume ausgegraut/deaktiviert sind."""

    def __init__(self):
        super().__init__(timeout=None)
        self.build_buttons()

    def build_buttons(self):
        self.clear_items()
        today = heute_key()
        eintrag = get_tag_eintrag(today)
        for slot in SLOTS:
            leute = eintrag.get(slot, [])
            voll = len(leute) >= MAX_PLAETZE_PRO_SLOT
            button = discord.ui.Button(
                label=f"{slot_label(slot)} ({len(leute)}/{MAX_PLAETZE_PRO_SLOT})",
                style=discord.ButtonStyle.success if not voll else discord.ButtonStyle.secondary,
                disabled=voll,
                custom_id=f"wache_slot_{slot}",
            )
            button.callback = self._make_callback(slot)
            self.add_item(button)

    def _make_callback(self, slot: str):
        async def callback(interaction: discord.Interaction):
            await self.handle_click(interaction, slot)
        return callback

    async def handle_click(self, interaction: discord.Interaction, slot: str):
        today = heute_key()
        eintrag = get_tag_eintrag(today)
        uid = str(interaction.user.id)

        bestehender_slot = finde_slot_von_user(eintrag, uid)
        if bestehender_slot:
            await interaction.response.send_message(
                f"❌ Du bist heute schon für **{slot_label(bestehender_slot)}** eingetragen. "
                f"Mit `/wache_austragen` kannst du dich zuerst wieder austragen.",
                ephemeral=True
            )
            return

        liste = eintrag.setdefault(slot, [])
        if len(liste) >= MAX_PLAETZE_PRO_SLOT:
            await interaction.response.send_message(
                "❌ Dieser Zeitraum ist leider gerade eben voll geworden. Bitte wähle einen anderen.",
                ephemeral=True
            )
            self.build_buttons()
            try:
                await interaction.message.edit(embed=build_wache_embed(today, interaction.guild), view=self)
            except Exception:
                pass
            return

        liste.append(uid)
        save_data(data)
        self.build_buttons()

        embed = build_wache_embed(today, interaction.guild)
        await interaction.response.edit_message(embed=embed, view=self)
        await interaction.followup.send(f"🟢 Du bist eingetragen für **{slot_label(slot)}**!", ephemeral=True)

        await update_wache_liste(interaction.guild)


wache_view = WacheView()

async def refresh_wache_nachricht(guild: discord.Guild):
    if not data.get("channel_stempel"):
        return
    kanal = guild.get_channel(int(data["channel_stempel"]))
    if not kanal:
        return

    today = heute_key()
    wache_view.build_buttons()
    embed = build_wache_embed(today, guild)

    msg_id = data.get("stempel_nachricht_id")
    if msg_id:
        try:
            msg = await kanal.fetch_message(int(msg_id))
            await msg.edit(embed=embed, view=wache_view)
            return
        except Exception as e:
            print(f"Alte Routenwache-Nachricht nicht gefunden, poste neu: {e}")

    msg = await kanal.send(embed=embed, view=wache_view)
    data["stempel_nachricht_id"] = str(msg.id)
    save_data(data)

async def update_wache_liste(guild: discord.Guild):
    if not data.get("channel_stempel_liste"):
        return
    kanal = guild.get_channel(int(data["channel_stempel_liste"]))
    if not kanal:
        return

    today = heute_key()
    embed = build_wache_embed(today, guild)

    msg_id = data.get("stempel_liste_nachricht_id")
    if msg_id:
        try:
            msg = await kanal.fetch_message(int(msg_id))
            await msg.edit(embed=embed)
            return
        except Exception as e:
            print(f"Alte Routenwache-Übersicht nicht gefunden, poste neu: {e}")

    msg = await kanal.send(embed=embed)
    data["stempel_liste_nachricht_id"] = str(msg.id)
    save_data(data)


# ─── Slash-Commands ────────────────────────────────────────────────────────────
# Hinweis: /wache_channel_setzen, /wache_liste_channel_setzen und /wache_posten
# bleiben Admin/Leitungs-Befehle (Setup). /wache_eintragen, /wache_austragen und
# /meine_wache haben KEINE Berechtigungs-Einschränkung – jedes Mitglied kann sie
# nutzen (auch für andere Mitglieder).

@tree.command(name="wache_channel_setzen", description="Setzt den Channel für die Routenwache-Buttons")
@app_commands.describe(channel="Der Channel wo die Zeitraum-Buttons gepostet werden")
@app_commands.check(ist_admin_oder_leitung)
async def wache_channel_setzen(interaction: discord.Interaction, channel: discord.TextChannel):
    data["channel_stempel"] = channel.id
    data["stempel_nachricht_id"] = None
    save_data(data)
    await interaction.response.send_message(f"✅ Routenwache-Channel gesetzt: {channel.mention}", ephemeral=True)
    await refresh_wache_nachricht(interaction.guild)

@tree.command(name="wache_liste_channel_setzen", description="Setzt den Channel für die Routenwache-Übersicht")
@app_commands.describe(channel="Der Channel wo die Tages-Übersicht gepostet wird")
@app_commands.check(ist_admin_oder_leitung)
async def wache_liste_channel_setzen(interaction: discord.Interaction, channel: discord.TextChannel):
    data["channel_stempel_liste"] = channel.id
    data["stempel_liste_nachricht_id"] = None
    save_data(data)
    await interaction.response.send_message(f"✅ Routenwache-Übersicht-Channel gesetzt: {channel.mention}", ephemeral=True)
    await update_wache_liste(interaction.guild)

@tree.command(name="wache_posten", description="Postet oder aktualisiert die Routenwache-Nachricht (Zeitraum-Buttons)")
@app_commands.check(ist_admin_oder_leitung)
async def wache_posten(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    await refresh_wache_nachricht(interaction.guild)
    await update_wache_liste(interaction.guild)
    await interaction.followup.send("✅ Routenwache-Nachricht gepostet/aktualisiert.", ephemeral=True)

WACHE_CHOICES = [app_commands.Choice(name=slot_label(s), value=s) for s in SLOTS]

@tree.command(name="wache_eintragen", description="Trägt ein Mitglied manuell in einen heutigen Zeitraum ein")
@app_commands.describe(mitglied="Das Mitglied", zeitraum="Der Zeitraum")
@app_commands.choices(zeitraum=WACHE_CHOICES)
async def wache_eintragen(interaction: discord.Interaction, mitglied: discord.Member, zeitraum: app_commands.Choice[str]):
    today = heute_key()
    eintrag = get_tag_eintrag(today)
    uid = str(mitglied.id)
    slot = zeitraum.value

    bestehender_slot = finde_slot_von_user(eintrag, uid)
    if bestehender_slot:
        await interaction.response.send_message(
            f"❌ {mitglied.mention} ist heute schon für **{slot_label(bestehender_slot)}** eingetragen.",
            ephemeral=True
        )
        return

    liste = eintrag.setdefault(slot, [])
    if len(liste) >= MAX_PLAETZE_PRO_SLOT:
        await interaction.response.send_message(f"❌ **{slot_label(slot)}** ist bereits voll.", ephemeral=True)
        return

    liste.append(uid)
    save_data(data)

    await interaction.response.send_message(f"✅ {mitglied.mention} wurde für **{slot_label(slot)}** eingetragen.", ephemeral=True)
    await refresh_wache_nachricht(interaction.guild)
    await update_wache_liste(interaction.guild)

@tree.command(name="wache_austragen", description="Trägt dich (oder ein anderes Mitglied) aus der heutigen Routenwache aus")
@app_commands.describe(mitglied="Optional: anderes Mitglied austragen (Standard: du selbst)")
async def wache_austragen(interaction: discord.Interaction, mitglied: discord.Member = None):
    ziel = mitglied or interaction.user
    today = heute_key()
    eintrag = get_tag_eintrag(today)
    uid = str(ziel.id)

    gefundener_slot = finde_slot_von_user(eintrag, uid)
    if not gefundener_slot:
        await interaction.response.send_message(f"❌ {ziel.mention} ist heute für keinen Zeitraum eingetragen.", ephemeral=True)
        return

    eintrag[gefundener_slot].remove(uid)
    save_data(data)

    await interaction.response.send_message(f"✅ {ziel.mention} wurde aus **{slot_label(gefundener_slot)}** ausgetragen.", ephemeral=True)
    await refresh_wache_nachricht(interaction.guild)
    await update_wache_liste(interaction.guild)

@tree.command(name="meine_wache", description="Zeigt deinen heutigen Routenwache-Status")
async def meine_wache(interaction: discord.Interaction):
    today = heute_key()
    eintrag = get_tag_eintrag(today)
    uid = str(interaction.user.id)
    slot = finde_slot_von_user(eintrag, uid)

    if slot:
        text = f"🟢 Du bist heute für **{slot_label(slot)}** eingetragen."
    else:
        text = "🔴 Du bist heute für keinen Zeitraum eingetragen."

    await interaction.response.send_message(f"**Deine Routenwache ({today})**\n{text}", ephemeral=True)

@tree.command(name="channels", description="Zeigt die aktuell gesetzten Channels für die Routenwache")
@app_commands.check(ist_admin_oder_leitung)
async def channels_info(interaction: discord.Interaction):
    stempel_ch = interaction.guild.get_channel(int(data["channel_stempel"])) if data.get("channel_stempel") else None
    stempel_liste_ch = interaction.guild.get_channel(int(data["channel_stempel_liste"])) if data.get("channel_stempel_liste") else None

    await interaction.response.send_message(
        f"**Aktuelle Einstellungen – Routenwache:**\n\n"
        f"Routenwache (Buttons):  {stempel_ch.mention if stempel_ch else '❌ Nicht gesetzt – /wache_channel_setzen benutzen'}\n"
        f"Routenwache-Übersicht:  {stempel_liste_ch.mention if stempel_liste_ch else '❌ Nicht gesetzt – /wache_liste_channel_setzen benutzen'}",
        ephemeral=True
    )


# ════════════════════════════════════════════════════════════════════════════
# 🌙  TAGESWECHSEL (automatischer Reset um Mitternacht)
# ════════════════════════════════════════════════════════════════════════════
letzter_bekannter_tag = None

@tasks.loop(seconds=30)
async def tageswechsel_check():
    global letzter_bekannter_tag
    heute = heute_key()
    if letzter_bekannter_tag != heute:
        letzter_bekannter_tag = heute
        for guild in bot.guilds:
            try:
                await refresh_wache_nachricht(guild)
                await update_wache_liste(guild)
                print(f"🌙 Tageswechsel erkannt, Routenwache für {heute} neu aufgesetzt.")
            except Exception as e:
                print(f"❌ Fehler beim Tageswechsel: {e}")

@tageswechsel_check.before_loop
async def before_tageswechsel_check():
    await bot.wait_until_ready()


# ════════════════════════════════════════════════════════════════════════════
# 🎯  BOT EVENTS
# ════════════════════════════════════════════════════════════════════════════

@bot.event
async def on_ready():
    global letzter_bekannter_tag
    print(f"Bot online: {bot.user}")

    try:
        if GUILD_ID:
            guild_obj = discord.Object(id=int(GUILD_ID))
            tree.copy_global_to(guild=guild_obj)
            synced = await tree.sync(guild=guild_obj)
            print(f"✅ {len(synced)} Commands SOFORT auf Guild {GUILD_ID} gesynct: {[c.name for c in synced]}")
        else:
            print("⚠️ Keine GUILD_ID gesetzt — sync läuft global (kann bis zu 1h dauern).")

        synced_global = await tree.sync()
        print(f"✅ {len(synced_global)} Commands global gesynct: {[c.name for c in synced_global]}")
    except Exception as e:
        print(f"❌ FEHLER beim Sync: {e}")

    bot.add_view(wache_view)
    letzter_bekannter_tag = heute_key()

    for guild in bot.guilds:
        try:
            await refresh_wache_nachricht(guild)
            await update_wache_liste(guild)
            print("✅ Routenwache-Nachricht/Übersicht aufgesetzt.")
        except Exception as e:
            print(f"❌ Fehler beim Auto-Posten der Nachrichten: {e}")

    if not tageswechsel_check.is_running():
        tageswechsel_check.start()

    print("Bot ist bereit!")

@bot.event
async def on_member_remove(member: discord.Member):
    """Entfernt automatisch alle Routenwache-Einträge eines Mitglieds,
    sobald es den Server verlässt (Leave oder Kick)."""
    uid = str(member.id)
    tage = data.get("tage", {})
    geaendert = False

    for datum, eintrag in tage.items():
        for slot, liste in eintrag.items():
            if uid in liste:
                liste.remove(uid)
                geaendert = True

    if not geaendert:
        return

    save_data(data)
    print(f"🧹 Routenwache-Einträge von {member} ({uid}) entfernt (Server verlassen).")

    try:
        await refresh_wache_nachricht(member.guild)
        await update_wache_liste(member.guild)
    except Exception as e:
        print(f"❌ Fehler beim Aktualisieren nach Austritt: {e}")

@bot.event
async def on_app_command_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.CheckFailure):
        await interaction.response.send_message("Du hast keine Berechtigung für diesen Befehl.", ephemeral=True)
    else:
        print(f"Command Error: {error}")
        try:
            await interaction.response.send_message("Ein Fehler ist aufgetreten.", ephemeral=True)
        except Exception:
            pass


# ════════════════════════════════════════════════════════════════════════════
# 🚀  START
# ════════════════════════════════════════════════════════════════════════════
bot.run(TOKEN)
