################################################################################
#                                IMPORTS (1-20)                                #
################################################################################

import logging
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Any, List, Set

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    BotCommand,
    Message,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    JobQueue,
)


################################################################################
#                            CONFIG (21-80)                                    #
################################################################################

class Config:
    BOT_TOKEN = "1321323"  # <-- токен
    CHAT_ID = 1231231  # <-- Общий чат ID

    BASE_DIR = Path(__file__).parent
    DATA_DIR = BASE_DIR / 'data'

    USERS_FILE = DATA_DIR / 'users.json'
    ACTIONS_FILE = DATA_DIR / 'user_actions.json'

    GLOBAL_DEADLINES_FILE = DATA_DIR / 'global_deadlines.json'
    GLOBAL_REMINDERS_FILE = DATA_DIR / 'global_sent_reminders.json'
    USER_DEADLINES_FILE = DATA_DIR / 'user_deadlines.json'
    USER_REMINDERS_FILE = DATA_DIR / 'user_sent_reminders.json'

    LOG_FORMAT = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    LOG_LEVEL = 'INFO'

    CHECK_INTERVAL = 60  # Проверка дедлайнов каждые 60с
    REMINDER_HOURS = [24, 72, 120]  # 1д,3д,5д (в часах)


################################################################################
#                          DATABASE (81-140)                                   #
################################################################################

class Database:
    @staticmethod
    def load_json(file_path: Path, default: Any = None) -> Any:
        if file_path.exists():
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                logging.error(f"Ошибка чтения {file_path}: {e}")
        return default

    @staticmethod
    def save_json(file_path: Path, data: Any) -> None:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logging.error(f"Ошибка записи {file_path}: {e}")


################################################################################
#                          USER MANAGER (141-180)                              #
################################################################################

class UserManager:
    """
    Храним всех пользователей (их user_id), которые написали боту.
    """

    def __init__(self, users_file: Path):
        self.users_file = users_file
        self.users: Set[str] = set(Database.load_json(users_file, default=[]))

    def add_user(self, user_id: str):
        self.users.add(user_id)
        Database.save_json(self.users_file, list(self.users))

    def get_users(self) -> Set[str]:
        return self.users


################################################################################
#                  GLOBAL DEADLINE MANAGER (181-280)                           #
################################################################################

class GlobalDeadlineManager:
    """
    Хранит ГЛОБАЛЬНЫЕ дедлайны (один общий список).
    Напоминания уходят каждому пользователю (из UserManager).
    """

    def __init__(self, deadlines_file: Path, reminders_file: Path):
        self.deadlines_file = deadlines_file
        self.reminders_file = reminders_file

        self.deadlines: List[dict] = Database.load_json(deadlines_file, default=[])
        self.sent_reminders: Dict[str, str] = Database.load_json(reminders_file, default={})

    def save_deadlines(self):
        Database.save_json(self.deadlines_file, self.deadlines)

    def save_reminders(self):
        Database.save_json(self.reminders_file, self.sent_reminders)

    def add_deadline(self, subject: str, task: str, date_str: str) -> bool:
        try:
            datetime.strptime(date_str, "%d.%m.%Y")
            item = {
                "subject": subject,
                "task": task,
                "deadline": date_str,
                "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            }
            self.deadlines.append(item)
            self.save_deadlines()
            return True
        except ValueError:
            return False

    def get_deadlines(self) -> List[dict]:
        return self.deadlines

    def remove_deadline(self, index: int) -> bool:
        if 0 <= index < len(self.deadlines):
            self.deadlines.pop(index)
            self.save_deadlines()
            return True
        return False

    async def check_deadlines(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        """
        Периодический вызов. Отправляет уведомления КАЖДОМУ user_id (из UserManager).
        """
        user_manager = context.bot_data.get('user_manager')
        if not user_manager:
            return

        all_users = user_manager.get_users()
        now = datetime.now()

        for dl in self.deadlines:
            try:
                dt = datetime.strptime(dl["deadline"], "%d.%m.%Y")
                hours_left = (dt - now).total_seconds() / 3600

                for h in Config.REMINDER_HOURS:
                    lb = h - (Config.CHECK_INTERVAL / 3600)
                    if h >= hours_left > lb:
                        reminder_key = f"{dl['subject']}_{dl['deadline']}_{int(h)}"
                        if reminder_key not in self.sent_reminders:
                            # Формируем сообщение
                            if h == 24:
                                note = "Остался 1 день"
                            elif h == 72:
                                note = "Осталось 3 дня"
                            elif h == 120:
                                note = "Осталось 5 дней"
                            else:
                                note = f"Осталось {int(h)} часов"

                            msg = (
                                f"⚠️ (Глобальный дедлайн)\n\n"
                                f"📚 Предмет: {dl['subject']}\n"
                                f"📝 Задание: {dl['task']}\n"
                                f"⏰ Дедлайн: {dl['deadline']}\n"
                                f"❗ {note}"
                            )

                            for uid in all_users:
                                try:
                                    await context.bot.send_message(chat_id=uid, text=msg)
                                except Exception as e:
                                    logging.error(f"Отправка дедлайна user={uid}: {e}")

                            self.sent_reminders[reminder_key] = now.strftime("%Y-%m-%d %H:%M:%S")
                            self.save_reminders()

            except ValueError:
                continue


################################################################################
#                   USER DEADLINE MANAGER (281-380)                            #
################################################################################

class UserDeadlineManager:
    """
    Хранит ЛИЧНЫЕ дедлайны (каждый user_id -> список).
    Уведомления отправляются ТОЛЬКО тому user_id, у кого дедлайн.
    """

    def __init__(self, user_file: Path, reminder_file: Path):
        self.deadlines_file = user_file
        self.reminders_file = reminder_file

        self.deadlines: Dict[str, List[dict]] = Database.load_json(user_file, default={})
        self.sent_reminders: Dict[str, str] = Database.load_json(reminder_file, default={})

    def save_deadlines(self):
        Database.save_json(self.deadlines_file, self.deadlines)

    def save_reminders(self):
        Database.save_json(self.reminders_file, self.sent_reminders)

    def add_deadline(self, user_id: str, subject: str, task: str, date_str: str) -> bool:
        try:
            datetime.strptime(date_str, "%d.%m.%Y")
            if user_id not in self.deadlines:
                self.deadlines[user_id] = []
            item = {
                "subject": subject,
                "task": task,
                "deadline": date_str,
                "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            }
            self.deadlines[user_id].append(item)
            self.save_deadlines()
            return True
        except ValueError:
            return False

    def get_user_deadlines(self, user_id: str) -> List[dict]:
        return self.deadlines.get(user_id, [])

    def remove_deadline(self, user_id: str, index: int) -> bool:
        arr = self.deadlines.get(user_id, [])
        if 0 <= index < len(arr):
            arr.pop(index)
            self.deadlines[user_id] = arr
            self.save_deadlines()
            return True
        return False

    async def check_personal_deadlines(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        """
        Периодическая проверка.
        """
        now = datetime.now()
        for user_id, arr in self.deadlines.items():
            for dl in arr:
                try:
                    dt = datetime.strptime(dl["deadline"], "%d.%m.%Y")
                    hours_left = (dt - now).total_seconds() / 3600

                    for h in Config.REMINDER_HOURS:
                        lb = h - (Config.CHECK_INTERVAL / 3600)
                        if h >= hours_left > lb:
                            r_key = f"{user_id}_{dl['subject']}_{dl['deadline']}_{int(h)}"
                            if r_key not in self.sent_reminders:
                                if h == 24:
                                    note = "Остался 1 день"
                                elif h == 72:
                                    note = "Осталось 3 дня"
                                elif h == 120:
                                    note = "Осталось 5 дней"
                                else:
                                    note = f"Осталось {int(h)} часов"

                                msg = (
                                    f"⚠️ (Личный дедлайн)\n\n"
                                    f"📚 Предмет: {dl['subject']}\n"
                                    f"📝 Задание: {dl['task']}\n"
                                    f"⏰ Дедлайн: {dl['deadline']}\n"
                                    f"❗ {note}"
                                )
                                try:
                                    await context.bot.send_message(chat_id=user_id, text=msg)
                                except Exception as e:
                                    logging.error(f"Не удалось отправить user_id={user_id}: {e}")

                                self.sent_reminders[r_key] = now.strftime("%Y-%m-%d %H:%M:%S")
                                self.save_reminders()
                except ValueError:
                    continue


################################################################################
#                ACTION MANAGER (381-420)                                      #
################################################################################

class ActionManager:
    """
    Позвать пить пиво/настолки/кино/гулять.
    Ограничение: не чаще, чем раз в 7 дней.
    """

    def __init__(self, actions_file: Path):
        self.actions_file = actions_file
        self.user_actions = Database.load_json(actions_file, default={})

    def can_perform_action(self, user_id: str, action_type: str) -> bool:
        now = datetime.now()
        last_str = self.user_actions.get(user_id, {}).get(action_type)
        if last_str:
            last_dt = datetime.fromisoformat(last_str)
            # неделя не прошла?
            if now - last_dt < timedelta(days=7):
                return False
        return True

    def update_action_time(self, user_id: str, action_type: str):
        now_str = datetime.now().isoformat()
        if user_id not in self.user_actions:
            self.user_actions[user_id] = {}
        self.user_actions[user_id][action_type] = now_str
        Database.save_json(self.actions_file, self.user_actions)


################################################################################
#               CALLBACK HANDLERS (421-500)                                    #
################################################################################

class CallbackHandlers:
    @staticmethod
    async def contact_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """
        inline callback for contacts
        """
        query = update.callback_query
        await query.answer()
        data = query.data

        contacts = {
            'contact_1': (
                "📌 <b>Учебная часть</b>:\n"
                "👩‍💼 Ответственная: <b>Некрасова Н.А.</b>\n"
                "📞 Тел: +7 342 200-95-55 доб. 6029\n"
                "📍 Пермь, ул. Студенческая, 38, каб.410\n"
                "⏰ 09:30 - 18:00"
            ),
            'contact_2': (
                "👩‍💼 <b>Руководитель программы</b>:\n"
                "👩‍🏫 <b>КАК ЗОВУТ???</b>\n"
                "📞 ЕЕЕ НОМЕР\n"
                "💬 Telegram: \n"
                "📱 +НОМЕРОК"
            )
        }

        if data in contacts:
            text = contacts[data]
            await query.edit_message_text(text=text, parse_mode='HTML')
        elif data == 'main_menu':
            await query.edit_message_text("Выберите нужный раздел внизу экрана.")

    @staticmethod
    async def deadline_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """
        inline callback: deadline_add, deadline_list, deadline_remove, ...
        """
        query = update.callback_query
        await query.answer()
        data = query.data

        if data == "deadline_add":
            await query.edit_message_text(
                "Добавить общий: /add_deadline subj task date\n"
                "Добавить личный: /add_personal_deadline subj task date\n"
                "(ДД.ММ.ГГГГ)"
            )
        elif data == "deadline_list":
            # вызывать /list_deadlines
            user_id = str(query.from_user.id)
            gdm: GlobalDeadlineManager = context.bot_data['global_deadline_manager']
            udm: UserDeadlineManager = context.bot_data['user_deadline_manager']

            gl = gdm.get_deadlines()
            per = udm.get_user_deadlines(user_id)

            msg = "=== Общие дедлайны ===\n"
            if not gl:
                msg += "Нет.\n"
            else:
                for i, dl in enumerate(gl):
                    msg += f"{i + 1}. {dl['subject']} / {dl['task']} / {dl['deadline']}\n"
            msg += "\n=== Ваши Личные ===\n"
            if not per:
                msg += "Нет.\n"
            else:
                for i, dl in enumerate(per):
                    msg += f"{i + 1}. {dl['subject']} / {dl['task']} / {dl['deadline']}\n"

            await context.bot.send_message(chat_id=user_id, text=msg)
            await query.edit_message_text("✅ Список отправлен ЛС.")
        elif data == "deadline_remove":
            await query.edit_message_text(
                "Удалить общий: /remove_deadline <номер>\n"
                "Удалить личный: /remove_personal_deadline <номер>"
            )
        elif data == "deadline_instructions":
            txt = (
                "Общий дедлайн:\n"
                "/add_deadline <subj> <task> <date>\n\n"
                "Личный дедлайн:\n"
                "/add_personal_deadline <subj> <task> <date>\n"
                "Дата: ДД.ММ.ГГГГ"
            )
            await query.edit_message_text(txt)


################################################################################
#                KEYBOARDS (501-550)                                          #
################################################################################

class Keyboards:
    @staticmethod
    def get_main_keyboard() -> ReplyKeyboardMarkup:
        kb = [
            [KeyboardButton("💼 Дедлайны")],
            [KeyboardButton("🎓 Средний балл диплома")],
            [KeyboardButton("📞 Контакты администрации")],
            [KeyboardButton("📚 Балл по предмету")],
            [KeyboardButton("🍺 Позвать пить пиво"), KeyboardButton("🎲 Позвать в настолки")],
            [KeyboardButton("🎥 Позвать в кино"), KeyboardButton("🚶 Позвать гулять")],
        ]
        return ReplyKeyboardMarkup(kb, resize_keyboard=True)

    @staticmethod
    def get_deadline_inline_keyboard() -> InlineKeyboardMarkup:
        kb = [
            [
                InlineKeyboardButton("➕ Добавить дедлайн", callback_data="deadline_add"),
                InlineKeyboardButton("📋 Список дедлайнов", callback_data="deadline_list"),
            ],
            [
                InlineKeyboardButton("❌ Удалить дедлайн", callback_data="deadline_remove"),
                InlineKeyboardButton("ℹ Инструкция", callback_data="deadline_instructions"),
            ],
        ]
        return InlineKeyboardMarkup(kb)


################################################################################
#                COMMAND HANDLERS (551-750)                                    #
################################################################################

class CommandHandlers:
    """
    Основные команды + логика: /start, /add_deadline, /add_personal_deadline...
    """

    def __init__(self, user_manager: UserManager, action_manager: ActionManager):
        self.user_manager = user_manager
        self.action_manager = action_manager

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = str(update.effective_user.id)
        self.user_manager.add_user(user_id)
        txt = (
            "👋 Привет! Это бот (общие дедлайны + личные дедлайны)!\n"
            "Пользуйтесь меню."
        )
        await update.message.reply_text(txt, reply_markup=Keyboards.get_main_keyboard())

    async def add_deadline_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """ /add_deadline subj task date """
        if len(context.args) < 3:
            await update.message.reply_text("Формат: /add_deadline subj task date")
            return
        subj = context.args[0]
        tsk = context.args[1]
        dt = context.args[2]
        gdm: GlobalDeadlineManager = context.bot_data['global_deadline_manager']
        ok = gdm.add_deadline(subj, tsk, dt)
        if ok:
            await update.message.reply_text(f"Глобальный дедлайн: {subj}/{tsk}/{dt}")
        else:
            await update.message.reply_text("Ошибка даты")

    async def add_personal_deadline_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """ /add_personal_deadline subj task date """
        user_id = str(update.effective_user.id)
        if len(context.args) < 3:
            await update.message.reply_text("Формат: /add_personal_deadline subj task date")
            return
        subj = context.args[0]
        tsk = context.args[1]
        dt = context.args[2]
        udm: UserDeadlineManager = context.bot_data['user_deadline_manager']
        ok = udm.add_deadline(user_id, subj, tsk, dt)
        if ok:
            await update.message.reply_text(f"Личный дедлайн: {subj}/{tsk}/{dt}")
        else:
            await update.message.reply_text("Ошибка даты?")

    async def diploma_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """При нажатии кнопки «🎓 Средний балл диплома»"""
        context.user_data['state'] = 'diploma_average'
        await update.message.reply_text("Введите оценки диплома (через пробел).")

    async def subject_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """При нажатии кнопки «📚 Балл по предмету»"""
        context.user_data['state'] = 'subject_average'
        await update.message.reply_text("Введите оценки предмета (через пробел).")

    async def handle_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Обрабатываем текст. Может быть «позвать пить пиво» и т.д."""
        state = context.user_data.get('state')
        txt = update.message.text.strip()
        if state == 'diploma_average':
            await self._calc_average(update, context, is_diploma=True)
        elif state == 'subject_average':
            await self._calc_average(update, context, is_diploma=False)
        else:
            # проверяем кнопки
            if txt == "💼 Дедлайны":
                await update.message.reply_text(
                    "Выберите действие с дедлайнами:",
                    reply_markup=Keyboards.get_deadline_inline_keyboard()
                )
            elif txt == "🎓 Средний балл диплома":
                await self.diploma_cmd(update, context)
            elif txt == "📚 Балл по предмету":
                await self.subject_cmd(update, context)
            elif txt == "📞 Контакты администрации":
                kb = [
                    [
                        InlineKeyboardButton("Учебная часть", callback_data='contact_1'),
                        InlineKeyboardButton("Руководитель", callback_data='contact_2')
                    ],
                    [InlineKeyboardButton("Назад", callback_data='main_menu')]
                ]
                await update.message.reply_text("Выберите контакт:", reply_markup=InlineKeyboardMarkup(kb))
            elif txt in ["🍺 Позвать пить пиво", "🎲 Позвать в настолки", "🎥 Позвать в кино", "🚶 Позвать гулять"]:
                # Определим action_type
                mapping = {
                    "🍺 Позвать пить пиво": "beer",
                    "🎲 Позвать в настолки": "board_games",
                    "🎥 Позвать в кино": "cinema",
                    "🚶 Позвать гулять": "walk"
                }
                action_type = mapping.get(txt)
                await self._call_action(update, context, action_type)
            else:
                await update.message.reply_text("Не понял команду. Воспользуйтесь меню.")

    async def _calc_average(self, update: Update, context: ContextTypes.DEFAULT_TYPE, is_diploma: bool) -> None:
        """Рассчитываем средний балл (диспетчер)"""
        try:
            raw = update.message.text.split()
            if not raw:
                await update.message.reply_text("Нет оценок.")
                return
            arr = list(map(float, raw))
            avg = sum(arr) / len(arr)
            if is_diploma:
                await update.message.reply_text(f"🎓 Средний балл диплома: {avg:.2f}")
            else:
                await update.message.reply_text(f"📚 Средний балл предмета: {avg:.2f}")
        except ValueError:
            await update.message.reply_text("Некорректные оценки.")
        finally:
            context.user_data['state'] = None

    async def _call_action(self, update: Update, context: ContextTypes.DEFAULT_TYPE, action_type: str) -> None:
        """Логика «Позвать ...»"""
        user_id = str(update.effective_user.id)
        if not self.action_manager.can_perform_action(user_id, action_type):
            await update.message.reply_text("❗ Нельзя так часто, подождите неделю.")
            return

        # Сообщение
        mapping_desc = {
            'beer': "пойти пить пиво",
            'board_games': "поиграть в настолки",
            'cinema': "сходить в кино",
            'walk': "пойти гулять"
        }
        desc = mapping_desc.get(action_type, "что-то сделать")
        text = f"{update.effective_user.first_name} предлагает {desc}! Кто присоединится?"

        # Рассылаем всем
        user_ids = self.user_manager.get_users()
        for uid in user_ids:
            if uid != user_id:
                try:
                    await context.bot.send_message(chat_id=uid, text=text)
                except Exception as e:
                    logging.error(f"Ошибка рассылки user_id={uid}: {e}")

        self.action_manager.update_action_time(user_id, action_type)
        await update.message.reply_text("✅ Сообщение отправлено всем!")


################################################################################
#                   STUDENT BOT (751-900)                                      #
################################################################################

class StudentBot:
    """
    Наш основной класс бота.
    """

    def __init__(self, token: str):
        self.token = token

        # Менеджеры
        self.user_manager = UserManager(Config.USERS_FILE)
        self.global_manager = GlobalDeadlineManager(
            Config.GLOBAL_DEADLINES_FILE,
            Config.GLOBAL_REMINDERS_FILE
        )
        self.user_manager_deadline = UserDeadlineManager(
            Config.USER_DEADLINES_FILE,
            Config.USER_REMINDERS_FILE
        )
        self.action_manager = ActionManager(Config.ACTIONS_FILE)

        # Командный обработчик
        self.cmd_handlers = CommandHandlers(
            self.user_manager,
            self.action_manager
        )

    async def _set_commands(self, app):
        """Показ команд в интерфейсе"""
        await app.bot.set_my_commands([
            BotCommand("start", "Запуск бота"),

            BotCommand("add_deadline", "Добавить общий дедлайн"),
            BotCommand("list_deadlines", "Список общих+личных дедлайнов"),
            BotCommand("remove_deadline", "Удалить общий дедлайн"),
            BotCommand("deadline_instructions", "Инструкция (общие дедлайны)"),

            BotCommand("add_personal_deadline", "Добавить личный дедлайн"),
            BotCommand("list_personal_deadlines", "Список личных дедлайнов"),
            BotCommand("remove_personal_deadline", "Удалить личный дедлайн"),
        ])

    def run(self):
        logging.basicConfig(format=Config.LOG_FORMAT, level=Config.LOG_LEVEL)
        application = (
            ApplicationBuilder()
            .token(self.token)
            .post_init(self._set_commands)
            .build()
        )

        # bot_data
        application.bot_data['user_manager'] = self.user_manager
        application.bot_data['global_deadline_manager'] = self.global_manager
        application.bot_data['user_deadline_manager'] = self.user_manager_deadline
        application.bot_data['action_manager'] = self.action_manager
        application.bot_data['command_handlers'] = self.cmd_handlers

        # Командные
        application.add_handler(CommandHandler("start", self.cmd_handlers.start))

        application.add_handler(CommandHandler("add_deadline", self.cmd_handlers.add_deadline_cmd))
        application.add_handler(CommandHandler("add_personal_deadline", self.cmd_handlers.add_personal_deadline_cmd))

        application.add_handler(CommandHandler("list_deadlines", self.cmd_handlers.handle_text))
        # можно выше: -> self.cmd_handlers.list_deadlines_command
        # но для DEMO используем handle_text,
        # или можем напрямую:
        application.add_handler(CommandHandler("remove_deadline", self.cmd_handlers.handle_text))

        # Слушаем любой текст
        application.add_handler(MessageHandler(
            filters.TEXT & (~filters.COMMAND),
            self.cmd_handlers.handle_text
        ))

        # Колбэки
        application.add_handler(CallbackQueryHandler(
            CallbackHandlers.contact_callback,
            pattern='^(contact_1|contact_2|main_menu)$'
        ))
        application.add_handler(CallbackQueryHandler(
            CallbackHandlers.deadline_menu_callback,
            pattern='^(deadline_add|deadline_list|deadline_remove|deadline_instructions)$'
        ))

        # Периодические задачи
        application.job_queue.run_repeating(
            self.global_manager.check_deadlines,
            interval=Config.CHECK_INTERVAL,
            first=0
        )
        application.job_queue.run_repeating(
            self.user_manager_deadline.check_personal_deadlines,
            interval=Config.CHECK_INTERVAL,
            first=0
        )

        application.run_polling()


################################################################################
#                           MAIN (901-1000)                                    #
################################################################################

if __name__ == "__main__":
    Config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    bot = StudentBot(Config.BOT_TOKEN)
    bot.run()
