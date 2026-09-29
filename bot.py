import ast
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands


# Читаем конфиг как набор литералов, не исполняя его содержимое.
CONFIG_FILE = Path(__file__).with_name("config.txt")
CONFIG_KEYS = (
    "TOKEN",
    "GUILD_ID",
    "RULES_CHANNEL_ID",
    "ANNOUNCE_CHANNEL_ID",
    "LOG_CHANNEL_ID",
    "MEMBER_ROLE_ID",
    "OWNER_ID",
)


def load_config() -> dict:
    try:
        config_tree = ast.parse(CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, SyntaxError) as error:
        raise RuntimeError("Не удалось прочитать config.txt как Python-конфиг.") from error

    values = {}
    for statement in config_tree.body:
        if not isinstance(statement, ast.Assign) or len(statement.targets) != 1:
            continue
        target = statement.targets[0]
        if not isinstance(target, ast.Name) or target.id not in CONFIG_KEYS:
            continue
        if target.id in values:
            raise RuntimeError(f"Поле {target.id} повторяется в config.txt.")
        try:
            values[target.id] = ast.literal_eval(statement.value)
        except (ValueError, TypeError) as error:
            raise RuntimeError(f"Поле {target.id} должно быть строкой или числом.") from error

    missing = [key for key in CONFIG_KEYS if key not in values]
    if missing:
        raise RuntimeError(f"В config.txt отсутствуют поля: {', '.join(missing)}")
    if not isinstance(values["TOKEN"], str) or not values["TOKEN"].strip():
        raise RuntimeError("В config.txt укажите токен бота в поле TOKEN.")
    for key in CONFIG_KEYS[1:]:
        if type(values[key]) is not int or values[key] <= 0:
            raise RuntimeError(f"В config.txt укажите положительный числовой ID в {key}.")
    return values


_config = load_config()
TOKEN = _config["TOKEN"]
GUILD_ID = _config["GUILD_ID"]
RULES_CHANNEL_ID = _config["RULES_CHANNEL_ID"]
ANNOUNCE_CHANNEL_ID = _config["ANNOUNCE_CHANNEL_ID"]
LOG_CHANNEL_ID = _config["LOG_CHANNEL_ID"]
MEMBER_ROLE_ID = _config["MEMBER_ROLE_ID"]
OWNER_ID = _config["OWNER_ID"]


# В Discord Developer Portal включите Server Members Intent и Message Content Intent:
# Bot → Privileged Gateway Intents. Остальные перечисленные intents включаются в коде.
intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.message_content = True
intents.dm_messages = True

bot = commands.Bot(command_prefix="!", intents=intents)
DATA_FILE = Path(__file__).with_name("data.json")
AVATAR_FILE = Path(__file__).with_name("266dfe8c-83ea-4e89-8876-00fc176698c3.jfif")

# Один и тот же custom_id используется на всех состояниях постоянной кнопки.
POLL_CUSTOM_ID = "poll_action"
PERSISTENT_VIEW_ADDED = False
COMMANDS_SYNCED = False


# Загружаем подтверждения знакомства с правилами; повреждённый или отсутствующий
# файл не мешает запуску бота.
def load_data() -> dict:
    try:
        with DATA_FILE.open("r", encoding="utf-8") as data_file:
            data = json.load(data_file)
        if isinstance(data, dict) and isinstance(data.get("acknowledged"), list):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {"acknowledged": []}


data = load_data()


# Сохраняем файл через временную копию, чтобы прерывание записи не испортило JSON.
def save_data() -> None:
    temporary_file = DATA_FILE.with_suffix(".json.tmp")
    with temporary_file.open("w", encoding="utf-8") as data_file:
        json.dump(data, data_file, ensure_ascii=False, indent=2)
    os.replace(temporary_file, DATA_FILE)


def acknowledgement_key(guild_id: int, user_id: int) -> str:
    return f"{guild_id}:{user_id}"


def has_acknowledged(guild_id: int, user_id: int) -> bool:
    return acknowledgement_key(guild_id, user_id) in data["acknowledged"]


# Ищем среди последних 200 сообщений первое сообщение владельца; остальные
# авторы и сообщения бота полностью игнорируются.
async def get_latest_rules(guild: discord.Guild) -> str:
    channel = guild.get_channel(RULES_CHANNEL_ID)
    if channel is None:
        try:
            channel = await guild.fetch_channel(RULES_CHANNEL_ID)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return "Правила ещё не заданы."

    if not isinstance(channel, discord.TextChannel):
        return "Правила ещё не заданы."

    try:
        async for message in channel.history(limit=200):
            if message.author.id != OWNER_ID or message.author.bot:
                continue

            rules = message.content.strip()
            if not rules and message.embeds:
                parts = []
                for embed in message.embeds:
                    if embed.title:
                        parts.append(embed.title)
                    if embed.description:
                        parts.append(embed.description)
                    parts.extend(field.value for field in embed.fields)
                rules = "\n\n".join(parts).strip()
            return rules[:4096] or "Правила ещё не заданы."
    except (discord.Forbidden, discord.HTTPException):
        return "Правила ещё не заданы."
    return "Правила ещё не заданы."


# Формируем embed с uid владельца панели в footer для проверки клика.
def make_panel_embed(user: discord.Member, rules: str) -> discord.Embed:
    embed = discord.Embed(
        title="Правила сервера",
        description=rules,
        color=discord.Color.green(),
    )
    embed.set_footer(text=f"uid:{user.id}")
    return embed


# Определяем состояние по роли и сохранённому подтверждению.
def poll_state(member: discord.Member) -> str:
    if discord.utils.get(member.roles, id=MEMBER_ROLE_ID) is not None:
        return "leave"
    if has_acknowledged(member.guild.id, member.id):
        return "join"
    return "acknowledge"


# Показываем кнопку, соответствующую состоянию пользователя.
def view_for_member(member: discord.Member) -> discord.ui.View:
    return PollView(poll_state(member))


# Извлекаем владельца панели и отклоняем клики других пользователей.
def panel_owner_id(interaction: discord.Interaction) -> Optional[int]:
    if interaction.message is None or not interaction.message.embeds:
        return None
    footer = interaction.message.embeds[0].footer.text or ""
    match = re.fullmatch(r"uid:(\d+)", footer)
    return int(match.group(1)) if match else None


# Считаем участников по роли, при необходимости учитывая уже обработанное
# действие до обновления локального кэша Discord.
def participant_count(
    guild: discord.Guild,
    member: Optional[discord.Member] = None,
    should_have_role: Optional[bool] = None,
) -> int:
    role = guild.get_role(MEMBER_ROLE_ID)
    if role is None:
        return 0
    count = len(role.members)
    if member is not None and should_have_role is not None:
        is_counted = any(role_member.id == member.id for role_member in role.members)
        if should_have_role and not is_counted:
            count += 1
        elif not should_have_role and is_counted:
            count -= 1
    return count


# Отправляем текст в заданный текстовый канал, используя cache или REST-запрос.
async def send_channel_message(
    guild: discord.Guild, channel_id: int, content: str
) -> None:
    channel = guild.get_channel(channel_id)
    if channel is None:
        try:
            channel = await guild.fetch_channel(channel_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return
    if not isinstance(channel, discord.TextChannel):
        return
    try:
        await channel.send(content)
    except (discord.Forbidden, discord.HTTPException):
        return


# Устанавливаем аватар только при изменении файла, чтобы не тратить лимит Discord.
async def sync_bot_avatar() -> None:
    if bot.user is None or not AVATAR_FILE.is_file():
        return

    try:
        image_bytes = AVATAR_FILE.read_bytes()
    except OSError:
        print("Не удалось прочитать файл аватара бота.")
        return

    image_hash = hashlib.sha256(image_bytes).hexdigest()
    if data.get("bot_avatar_sha256") == image_hash:
        return

    try:
        await bot.user.edit(avatar=image_bytes)
    except discord.HTTPException:
        print("Discord не принял файл аватара; бот продолжает работу.")
        return

    data["bot_avatar_sha256"] = image_hash
    save_data()
    print("Аватар бота обновлён.")


# Обновляем публичный канал и приватный лог после вступления или выхода.
async def announce_membership_change(
    guild: discord.Guild, member: discord.Member, joined: bool
) -> None:
    count = participant_count(guild, member, should_have_role=joined)
    if joined:
        announce_text = f"➕ {member.mention} присоединился к составу!"
        log_text = f"➕ {member.mention} (`{member.display_name}`) присоединился. Всего: {count}"
    else:
        announce_text = f"➖ {member.mention} покинул состав."
        log_text = f"➖ {member.mention} покинул состав. Всего: {count}"
    await send_channel_message(guild, ANNOUNCE_CHANNEL_ID, announce_text)
    await send_channel_message(guild, LOG_CHANNEL_ID, log_text)


# Выполняем действие DM-кнопки: в личном сообщении guild надо искать по конфигу,
# а владельца панели проверяем по uid из embed footer.
async def handle_panel_action(interaction: discord.Interaction) -> None:
    if panel_owner_id(interaction) != interaction.user.id:
        await interaction.response.send_message(
            "Это не ваше сообщение", ephemeral=True
        )
        return
    await interaction.response.defer()

    guild = bot.get_guild(GUILD_ID)
    if guild is None:
        await interaction.followup.send(
            "Сервер не найден. Проверьте GUILD_ID.", ephemeral=True
        )
        return
    member = guild.get_member(interaction.user.id)
    if member is None:
        try:
            member = await guild.fetch_member(interaction.user.id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            await interaction.followup.send(
                "Не удалось найти вас на настроенном сервере.", ephemeral=True
            )
            return

    state = poll_state(member)
    state_labels = {
        "acknowledge": "✅ Ознакомился с правилами",
        "join": "🟢 Присоединиться",
        "leave": "🔴 Покинуть состав",
    }
    displayed_label = None
    if interaction.message is not None:
        displayed_label = next(
            (
                component.label
                for row in interaction.message.components
                for component in row.children
                if component.custom_id == POLL_CUSTOM_ID
            ),
            None,
        )
    if displayed_label != state_labels[state]:
        await interaction.edit_original_response(view=PollView(state))
        return

    if state == "acknowledge":
        key = acknowledgement_key(guild.id, member.id)
        data["acknowledged"].append(key)
        save_data()
        await interaction.edit_original_response(view=PollView("join"))
        return

    role = guild.get_role(MEMBER_ROLE_ID)
    if role is None:
        await interaction.followup.send(
            "Роль участника не найдена. Проверьте MEMBER_ROLE_ID.", ephemeral=True
        )
        return

    if state == "join":
        try:
            await member.add_roles(role, reason="Пользователь вступил через панель")
        except (discord.Forbidden, discord.HTTPException):
            await interaction.followup.send(
                "Не удалось выдать роль. Проверьте права бота и порядок ролей.",
                ephemeral=True,
            )
            return
        await interaction.edit_original_response(view=PollView("leave"))
        await announce_membership_change(guild, member, joined=True)
        return

    try:
        await member.remove_roles(role, reason="Пользователь покинул состав")
    except (discord.Forbidden, discord.HTTPException):
        await interaction.followup.send(
            "Не удалось снять роль. Проверьте права бота и порядок ролей.",
            ephemeral=True,
        )
        return
    await interaction.edit_original_response(view=PollView("join"))
    await announce_membership_change(guild, member, joined=False)


# Persistent-view с одним неизменным custom_id меняет только подпись кнопки.
class PollView(discord.ui.View):
    def __init__(self, state: str = "acknowledge") -> None:
        super().__init__(timeout=None)
        labels = {
            "acknowledge": ("✅ Ознакомился с правилами", discord.ButtonStyle.success),
            "join": ("🟢 Присоединиться", discord.ButtonStyle.success),
            "leave": ("🔴 Покинуть состав", discord.ButtonStyle.danger),
        }
        label, style = labels[state]
        button = discord.ui.Button(
            label=label, style=style, custom_id=POLL_CUSTOM_ID
        )
        button.callback = self.poll_button
        self.add_item(button)

    async def poll_button(self, interaction: discord.Interaction) -> None:
        await handle_panel_action(interaction)


# После подключения восстанавливаем handler для старых DM и синхронизируем
# slash-команды только с настроенным сервером.
@bot.event
async def on_ready() -> None:
    global PERSISTENT_VIEW_ADDED, COMMANDS_SYNCED
    if not PERSISTENT_VIEW_ADDED:
        bot.add_view(PollView())
        PERSISTENT_VIEW_ADDED = True
    if not COMMANDS_SYNCED:
        await bot.tree.sync(guild=discord.Object(id=GUILD_ID))
        COMMANDS_SYNCED = True
    await sync_bot_avatar()


# При входе участника отправляем правила и персональную панель ему в ЛС.
@bot.event
async def on_member_join(member: discord.Member) -> None:
    if member.guild.id != GUILD_ID:
        return
    rules = await get_latest_rules(member.guild)
    embed = make_panel_embed(member, rules)
    try:
        dm_channel = await member.create_dm()
        await dm_channel.send(
            content="Ознакомьтесь с правилами сервера.",
            embed=embed,
            view=view_for_member(member),
        )
    except discord.Forbidden:
        await send_channel_message(
            member.guild,
            LOG_CHANNEL_ID,
            f"⚠️ У {member.mention} закрыты ЛС.",
        )


# Если участник покинул сервер с ролью, отражаем это в канале состава и логах.
@bot.event
async def on_member_remove(member: discord.Member) -> None:
    if member.guild.id != GUILD_ID:
        return
    if discord.utils.get(member.roles, id=MEMBER_ROLE_ID) is None:
        return
    count = participant_count(member.guild, member, should_have_role=False)
    await send_channel_message(
        member.guild,
        ANNOUNCE_CHANNEL_ID,
        f"🚪 {member.mention} покинул сервер.",
    )
    await send_channel_message(
        member.guild,
        LOG_CHANNEL_ID,
        f"🚪 {member.mention} (`{member.display_name}`) покинул сервер. Всего: {count}",
    )


# Создаём персональную панель вручную; доступна только администраторам сервера.
@bot.tree.command(name="panel", description="Создать персональную панель вступления")
@app_commands.guilds(discord.Object(id=GUILD_ID))
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
async def panel(
    interaction: discord.Interaction,
    user: Optional[discord.Member] = None,
) -> None:
    if not isinstance(interaction.user, discord.Member) or not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message(
            "Команда доступна только администраторам.", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)
    target = user or interaction.user
    rules = await get_latest_rules(interaction.guild)
    embed = make_panel_embed(target, rules)
    try:
        dm_channel = await target.create_dm()
        await dm_channel.send(
            content="Ознакомьтесь с правилами сервера.",
            embed=embed,
            view=view_for_member(target),
        )
    except discord.Forbidden:
        await send_channel_message(
            interaction.guild,
            LOG_CHANNEL_ID,
            f"⚠️ У {target.mention} закрыты ЛС.",
        )
        await interaction.edit_original_response(
            content=f"Не удалось отправить ЛС {target.mention}: личные сообщения закрыты."
        )
        return
    await interaction.edit_original_response(
        content=f"Опрос отправлен в ЛС {target.mention}.",
    )


# Показываем число участников, которым выдана настроенная роль.
@bot.tree.command(name="count", description="Показать число участников в составе")
@app_commands.guilds(discord.Object(id=GUILD_ID))
@app_commands.guild_only()
async def count(interaction: discord.Interaction) -> None:
    total = participant_count(interaction.guild)
    await interaction.response.send_message(f"Участников в составе: **{total}**.")


# Показываем актуальные правила из последнего сообщения владельца.
@bot.tree.command(name="rules", description="Показать текущие правила сервера")
@app_commands.guilds(discord.Object(id=GUILD_ID))
@app_commands.guild_only()
async def rules(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    current_rules = await get_latest_rules(interaction.guild)
    if not current_rules:
        await interaction.edit_original_response(
            content="Не удалось найти правила от владельца в настроенном канале.",
        )
        return
    embed = discord.Embed(title="Правила сервера", description=current_rules, color=discord.Color.green())
    await interaction.edit_original_response(embed=embed)


# Приватно показываем администраторам каналы сервера и их ID частями.
@bot.tree.command(name="channels", description="Показать каналы сервера и их ID")
@app_commands.guilds(discord.Object(id=GUILD_ID))
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
async def channels(interaction: discord.Interaction) -> None:
    if not isinstance(interaction.user, discord.Member) or not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message(
            "Команда доступна только администраторам.", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)
    channel_lines = [
        f"{channel.name} | {channel.type} | {channel.id}"
        for channel in sorted(interaction.guild.channels, key=lambda item: (item.position, item.id))
    ]
    if not channel_lines:
        await interaction.followup.send("На сервере нет доступных каналов.", ephemeral=True)
        return

    chunks = []
    current_chunk = []
    current_length = 0
    for line in channel_lines:
        if current_chunk and current_length + len(line) + 1 > 1700:
            chunks.append("\n".join(current_chunk))
            current_chunk = []
            current_length = 0
        current_chunk.append(line)
        current_length += len(line) + 1
    if current_chunk:
        chunks.append("\n".join(current_chunk))

    for index, chunk in enumerate(chunks, start=1):
        await interaction.followup.send(
            f"Каналы сервера ({index}/{len(chunks)}):\n```text\n{chunk}\n```",
            ephemeral=True,
        )


# Запускаем бота только после замены placeholder токена на новый секрет.
if __name__ == "__main__":
    if TOKEN == "ВСТАВЬ_НОВЫЙ_ТОКЕН_ПОСЛЕ_RESET":
        raise SystemExit("Сначала сбросьте токен в Developer Portal и обновите TOKEN в config.txt.")
    bot.run(TOKEN)