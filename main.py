import os
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
from openai import OpenAI

from db import (
    init_db, set_assistant_channel, get_assistant_channel,
    get_daily_usage, increment_daily_usage,
    get_protection_config, update_protection_config,
    list_banned_words, add_banned_word, remove_banned_word,
    list_allowed_domains, add_allowed_domain, remove_allowed_domain,
    list_bypass_roles, add_bypass_role, remove_bypass_role
)
from protection import handle_message, handle_member_update_roles

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.2")
FREE_DAILY_LIMIT = int(os.getenv("FREE_DAILY_LIMIT", "3"))

premium_raw = os.getenv("PREMIUM_GUILDS", "").strip()
PREMIUM_GUILDS = {int(x) for x in premium_raw.split(",") if x.strip().isdigit()}

ai = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.message_content = True  # لازم تفعّلها من Developer Portal

bot = commands.Bot(command_prefix="!", intents=intents)

# ✅ الصق الكود هنا
@bot.tree.command(name="premium_claim", description="Activate Premium for this server if you own the Premium role in the support server.")
async def premium_claim(interaction: discord.Interaction):
    import sqlite3, os
    support_guild_id = int(os.getenv("SUPPORT_GUILD_ID", "0"))
    premium_role_id = int(os.getenv("PREMIUM_ROLE_ID", "0"))

    if support_guild_id == 0 or premium_role_id == 0:
        await interaction.response.send_message("⚠️ Premium config missing in .env", ephemeral=True)
        return

    support_guild = bot.get_guild(support_guild_id)
    if not support_guild:
        await interaction.response.send_message("⚠️ Support server not found. Make sure the bot is inside the support server.", ephemeral=True)
        return

    member = support_guild.get_member(interaction.user.id)
    premium_role = support_guild.get_role(premium_role_id)

    if not member or not premium_role or premium_role not in member.roles:
        await interaction.response.send_message("❌ You don't have the Premium role in the support server.", ephemeral=True)
        return

    with sqlite3.connect("bot.db") as conn:
        c = conn.cursor()
        c.execute("CREATE TABLE IF NOT EXISTS premium_guilds (guild_id INTEGER PRIMARY KEY, expires_at TEXT)")
        c.execute("INSERT OR REPLACE INTO premium_guilds (guild_id, expires_at) VALUES (?, NULL)", (interaction.guild.id,))
        conn.commit()

    await interaction.response.send_message("✅ This server is now marked as **Premium**!", ephemeral=True)

def is_premium(guild_id: int) -> bool:
    return guild_id in PREMIUM_GUILDS

@bot.event
async def on_ready():
    await init_db()
    try:
        await bot.tree.sync()
        print(f"✅ Logged in as {bot.user}")
    except Exception as e:
        print("Sync error:", e)


# ====== حماية: فلترة الرسائل ======
@bot.event
async def on_message(message: discord.Message):
    if message.guild:
        cfg = await get_protection_config(message.guild.id)
        words = await list_banned_words(message.guild.id) if int(cfg.get("words_enabled") or 0) == 1 else []
        domains = await list_allowed_domains(message.guild.id) if (cfg.get("links_mode") == "all") else []
        await handle_message(message, cfg, words, domains, is_premium(message.guild.id))
    await bot.process_commands(message)

# ====== حماية: توزيع الرتب الخطير ======
@bot.event
async def on_member_update(before: discord.Member, after: discord.Member):
    cfg = await get_protection_config(after.guild.id)
    bypass = await list_bypass_roles(after.guild.id)
    await handle_member_update_roles(before, after, cfg, bypass, is_premium(after.guild.id))

# ====== أوامر المساعد AI ======
@bot.tree.command(name="setchannel", description="Set the assistant channel for this server")
@app_commands.checks.has_permissions(manage_guild=True)
async def setchannel(interaction: discord.Interaction, channel: discord.TextChannel):
    if not interaction.guild:
        return await interaction.response.send_message("Use this in a server.", ephemeral=True)
    await set_assistant_channel(interaction.guild.id, channel.id)
    await interaction.response.send_message(f"✅ Assistant channel set to {channel.mention}", ephemeral=True)

@bot.tree.command(name="usage", description="See how many free questions are left today")
async def usage(interaction: discord.Interaction):
    if not interaction.guild:
        return await interaction.response.send_message("Use this in a server.", ephemeral=True)

    if is_premium(interaction.guild.id):
        return await interaction.response.send_message("💎 Premium: Unlimited AI questions.", ephemeral=True)

    day = today_key_utc()
    used = await get_daily_usage(interaction.guild.id, day)
    left = max(FREE_DAILY_LIMIT - used, 0)
    await interaction.response.send_message(
        f"🆓 Free plan: {left} / {FREE_DAILY_LIMIT} questions left today.",
        ephemeral=True
    )

@bot.tree.command(name="ask", description="Ask the assistant (only works in the configured channel)")
async def ask(interaction: discord.Interaction, question: str):
    if not interaction.guild:
        return await interaction.response.send_message("Use this in a server.", ephemeral=True)

    ch_id = await get_assistant_channel(interaction.guild.id)
    if not ch_id:
        return await interaction.response.send_message("⚠️ Admins: /setchannel #channel", ephemeral=True)

    if interaction.channel_id != ch_id:
        ch = interaction.guild.get_channel(ch_id)
        return await interaction.response.send_message(
            f"❌ Use /ask in {ch.mention if ch else 'the assistant channel'}",
            ephemeral=True
        )

    if not is_premium(interaction.guild.id):
        day = today_key_utc()
        used_now = await get_daily_usage(interaction.guild.id, day)
        if used_now >= FREE_DAILY_LIMIT:
            return await interaction.response.send_message(
                f"🆓 Free limit reached ({FREE_DAILY_LIMIT}/day). Upgrade to Premium for unlimited AI.",
                ephemeral=True
            )
        new_count = await increment_daily_usage(interaction.guild.id, day)
        if new_count > FREE_DAILY_LIMIT:
            return await interaction.response.send_message(
                f"🆓 Free limit reached ({FREE_DAILY_LIMIT}/day). Upgrade to Premium for unlimited AI.",
                ephemeral=True
            )

    await interaction.response.defer()
    try:
        r = ai.responses.create(
            model=OPENAI_MODEL,
            input=f"You are a helpful Discord assistant. Answer clearly.\n\nUser: {question}"
        )
        text = (r.output_text or "").strip() or "⚠️ No response."
        await interaction.followup.send(text[:1900])
    except Exception:
        await interaction.followup.send("⚠️ Something went wrong. Try again later.")

# ====== أوامر الحماية (إعدادات) ======
def _need_guild(i: discord.Interaction):
    return i.guild is not None

@bot.tree.command(name="p_status", description="Show protection status (Free/Premium)")
async def p_status(interaction: discord.Interaction):
    if not _need_guild(interaction):
        return await interaction.response.send_message("Use this in a server.", ephemeral=True)
    cfg = await get_protection_config(interaction.guild.id)
    plan = "💎 Premium" if is_premium(interaction.guild.id) else "🆓 Free"
    await interaction.response.send_message(
        f"{plan}\n"
        f"Links: {'ON' if cfg['links_enabled'] else 'OFF'} (mode={cfg['links_mode']})\n"
        f"Spam: {'ON' if cfg['spam_enabled'] else 'OFF'} ({cfg['spam_max']}/{cfg['spam_window']}s)\n"
        f"Words: {'ON' if cfg['words_enabled'] else 'OFF'}\n"
        f"Mentions: {'ON' if cfg['mention_enabled'] else 'OFF'} (limit={cfg['mention_limit']})\n"
        f"Role protection: {'ON' if cfg['roles_enabled'] else 'OFF'} (Premium enforce)\n",
        ephemeral=True
    )

@bot.tree.command(name="p_logchannel", description="Set protection log channel")
@app_commands.checks.has_permissions(manage_guild=True)
async def p_logchannel(interaction: discord.Interaction, channel: discord.TextChannel):
    await update_protection_config(interaction.guild.id, log_channel_id=channel.id)
    await interaction.response.send_message(f"✅ Protection logs → {channel.mention}", ephemeral=True)

@bot.tree.command(name="p_links", description="Enable/disable link filter")
@app_commands.checks.has_permissions(manage_guild=True)
async def p_links(interaction: discord.Interaction, enabled: bool):
    await update_protection_config(interaction.guild.id, links_enabled=1 if enabled else 0)
    await interaction.response.send_message(f"✅ Links filter: {'ON' if enabled else 'OFF'}", ephemeral=True)

@bot.tree.command(name="p_links_mode", description="invites (Free) / all (Premium)")
@app_commands.checks.has_permissions(manage_guild=True)
async def p_links_mode(interaction: discord.Interaction, mode: str):
    mode = mode.lower().strip()
    if mode not in ("invites", "all"):
        return await interaction.response.send_message("Use: invites or all", ephemeral=True)
    if mode == "all" and not is_premium(interaction.guild.id):
        return await interaction.response.send_message("❌ 'all' links mode is Premium.", ephemeral=True)
    await update_protection_config(interaction.guild.id, links_mode=mode)
    await interaction.response.send_message(f"✅ Links mode set to: {mode}", ephemeral=True)

@bot.tree.command(name="p_domain_add", description="(Premium) allow a domain when links_mode=all")
@app_commands.checks.has_permissions(manage_guild=True)
async def p_domain_add(interaction: discord.Interaction, domain: str):
    if not is_premium(interaction.guild.id):
        return await interaction.response.send_message("❌ Domains allowlist is Premium.", ephemeral=True)
    await add_allowed_domain(interaction.guild.id, domain)
    await interaction.response.send_message(f"✅ Allowed domain added: {domain}", ephemeral=True)

@bot.tree.command(name="p_domain_list", description="List allowed domains")
async def p_domain_list(interaction: discord.Interaction):
    ds = await list_allowed_domains(interaction.guild.id)
    await interaction.response.send_message("✅ Allowed domains:\n" + ("\n".join(ds) if ds else "(none)"), ephemeral=True)

@bot.tree.command(name="p_word_add", description="Add a banned word")
@app_commands.checks.has_permissions(manage_guild=True)
async def p_word_add(interaction: discord.Interaction, word: str):
    await add_banned_word(interaction.guild.id, word)
    await interaction.response.send_message(f"✅ Banned word added: {word}", ephemeral=True)

@bot.tree.command(name="p_word_list", description="List banned words")
async def p_word_list(interaction: discord.Interaction):
    ws = await list_banned_words(interaction.guild.id)
    await interaction.response.send_message("✅ Banned words:\n" + ("\n".join(ws) if ws else "(none)"), ephemeral=True)

@bot.tree.command(name="p_spam_set", description="Set spam limit (max msgs / window seconds)")
@app_commands.checks.has_permissions(manage_guild=True)
async def p_spam_set(interaction: discord.Interaction, max_msgs: int, window_seconds: int):
    max_msgs = max(2, min(max_msgs, 30))
    window_seconds = max(3, min(window_seconds, 60))
    await update_protection_config(interaction.guild.id, spam_max=max_msgs, spam_window=window_seconds)
    await interaction.response.send_message(f"✅ Spam set: {max_msgs}/{window_seconds}s", ephemeral=True)

@bot.tree.command(name="p_roles", description="(Premium enforce) watch dangerous role grants")
@app_commands.checks.has_permissions(manage_guild=True)
async def p_roles(interaction: discord.Interaction, enabled: bool):
    await update_protection_config(interaction.guild.id, roles_enabled=1 if enabled else 0)
    msg = "✅ Role protection enabled (Premium will enforce, Free will log)." if enabled else "✅ Role protection disabled."
    await interaction.response.send_message(msg, ephemeral=True)

@bot.tree.command(name="p_bypassrole_add", description="(Premium) Add a bypass role for staff (skips role protection)")
@app_commands.checks.has_permissions(manage_guild=True)
async def p_bypassrole_add(interaction: discord.Interaction, role: discord.Role):
    if not is_premium(interaction.guild.id):
        return await interaction.response.send_message("❌ Bypass roles are Premium.", ephemeral=True)
    await add_bypass_role(interaction.guild.id, role.id)
    await interaction.response.send_message(f"✅ Bypass role added: {role.mention}", ephemeral=True)

if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_TOKEN missing in .env")

bot.run(DISCORD_TOKEN)
