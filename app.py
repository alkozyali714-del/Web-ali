import os
import hmac
import hashlib
import urllib.parse
import secrets
from datetime import datetime
from flask import Flask, request, jsonify, session, render_template
from flask_sqlalchemy import SQLAlchemy
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ConversationHandler, MessageHandler, filters, ContextTypes
)

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY')
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URL')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# ------------------- التوكن الجديد -------------------
BOT_TOKEN = "8945844684:AAE7YdLjf8zP-FEODT_Hg-ZBPQF1UdifPkQ"
# ----------------------------------------------------

ADMIN_ID = os.getenv('ADMIN_ID')

# حالات محادثة الشحن
AMOUNT, CONFIRM = range(2)

# ---------- نماذج قاعدة البيانات ----------
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    telegram_id = db.Column(db.String(50), unique=True, nullable=False)
    first_name = db.Column(db.String(100))
    username = db.Column(db.String(100))
    balance = db.Column(db.Float, default=0.0)
    referral_code = db.Column(db.String(20), unique=True)
    referrer_id = db.Column(db.String(50))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.telegram_id,
            "name": self.first_name,
            "username": self.username,
            "balance": f"{self.balance:.2f}",
            "joined": self.created_at.strftime("%Y-%m-%d")
        }

class Transaction(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.String(50), db.ForeignKey('user.telegram_id'))
    type = db.Column(db.String(20))  # deposit, withdraw, bonus
    amount = db.Column(db.Float)
    status = db.Column(db.String(20), default='pending')
    description = db.Column(db.String(200))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

# ---------- دوال الأمان والتحقق ----------
def verify_telegram_data(init_data: str) -> dict:
    try:
        parsed = urllib.parse.parse_qs(init_data)
        received_hash = parsed.pop('hash', [None])[0]
        if not received_hash:
            return None
        sorted_keys = sorted(parsed.keys())
        check_string = "\n".join(f"{k}={parsed[k][0]}" for k in sorted_keys)
        secret_key = hmac.new(
            key=b"WebAppData",
            msg=BOT_TOKEN.encode(),
            digestmod=hashlib.sha256
        ).digest()
        expected_hash = hmac.new(
            key=secret_key,
            msg=check_string.encode(),
            digestmod=hashlib.sha256
        ).hexdigest()
        if hmac.compare_digest(expected_hash, received_hash):
            return {k: v[0] for k, v in parsed.items()}
    except Exception:
        pass
    return None

def get_user_or_create(user_data):
    tg_id = user_data.get('id')
    user = User.query.filter_by(telegram_id=tg_id).first()
    if not user:
        code = secrets.token_hex(4).upper()
        user = User(
            telegram_id=tg_id,
            first_name=user_data.get('first_name', ''),
            username=user_data.get('username', ''),
            referral_code=code
        )
        db.session.add(user)
        db.session.commit()
    return user

# ---------- واجهات API للويب ----------
@app.route('/validate', methods=['POST'])
def validate():
    init_data = request.form.get('initData')
    if not init_data:
        return jsonify({"error": "Missing data"}), 400
    user_data = verify_telegram_data(init_data)
    if not user_data:
        return jsonify({"error": "Invalid auth"}), 403
    user = get_user_or_create(user_data)
    session['user_id'] = user.telegram_id
    return jsonify({"status": "success", "user": user.to_dict()}), 200

@app.route('/api/transactions')
def get_transactions():
    if 'user_id' not in session:
        return jsonify([]), 401
    txs = Transaction.query.filter_by(user_id=session['user_id'])\
        .order_by(Transaction.created_at.desc()).limit(20).all()
    return jsonify([{
        "id": t.id,
        "type": t.type,
        "amount": t.amount,
        "status": t.status,
        "desc": t.description,
        "date": t.created_at.strftime("%Y-%m-%d %H:%M")
    } for t in txs])

@app.route('/api/withdraw', methods=['POST'])
def withdraw():
    if 'user_id' not in session:
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json()
    amount = float(data.get('amount', 0))
    wallet = data.get('wallet')
    user = User.query.filter_by(telegram_id=session['user_id']).first()
    if amount <= 0 or amount > user.balance:
        return jsonify({"error": "رصيد غير كافٍ أو مبلغ غير صحيح"}), 400
    tx = Transaction(
        user_id=user.telegram_id,
        type='withdraw',
        amount=amount,
        status='pending',
        description=f"طلب سحب إلى {wallet}"
    )
    db.session.add(tx)
    user.balance -= amount
    db.session.commit()
    return jsonify({"status": "success", "new_balance": user.balance})

@app.route('/dashboard')
def dashboard():
    if 'user_id' not in session:
        return "يرجى فتح التطبيق من البوت", 401
    user = User.query.filter_by(telegram_id=session['user_id']).first()
    return render_template('dashboard.html', user=user)

# ---------- منطق البوت ----------
def get_main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👤 ملفي الشخصي", callback_data="profile"),
         InlineKeyboardButton("💰 شحن الرصيد", callback_data="recharge")],
        [InlineKeyboardButton("📊 تاريخ المعاملات", callback_data="history"),
         InlineKeyboardButton("🎁 نظام الإحالات", callback_data="referral")],
        [InlineKeyboardButton("📞 الدعم الفني", callback_data="support")]
    ])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    existing = User.query.filter_by(telegram_id=str(user.id)).first()
    if not existing:
        new_user = User(
            telegram_id=str(user.id),
            first_name=user.first_name,
            username=user.username,
            referral_code=secrets.token_hex(4).upper()
        )
        db.session.add(new_user)
        db.session.commit()
        existing = new_user
    text = f"""🚀 *مرحباً بك في النظام المالي المتطور* {user.first_name}!

اختر الخدمة التي ترغب بها من الأزرار أدناه:
📌 *رصيدك الحالي:* `{existing.balance:.2f} $`"""
    await update.message.reply_text(text, parse_mode='MarkdownV2', reply_markup=get_main_keyboard())

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = str(query.from_user.id)
    user = User.query.filter_by(telegram_id=user_id).first()
    if not user:
        await query.edit_message_text("حدث خطأ، يرجى إعادة /start")
        return

    data = query.data

    if data == "profile":
        text = f"""👤 *ملفي الشخصي*
🆔 المعرف: `{user.telegram_id}`
📛 الاسم: {user.first_name}
💰 الرصيد: `{user.balance:.2f} $`
🎫 كود الإحالة: `{user.referral_code}`"""
        await query.edit_message_text(text, parse_mode='MarkdownV2',
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 الرجوع", callback_data="back")]]))

    elif data == "history":
        txs = Transaction.query.filter_by(user_id=user_id).order_by(Transaction.created_at.desc()).limit(5).all()
        if not txs:
            text = "📭 *لا توجد معاملات حتى الآن.*"
        else:
            lines = ["📊 *آخر المعاملات:*"]
            for tx in txs:
                sign = "+" if tx.type == "deposit" else "-"
                emoji = "✅" if tx.status == "completed" else "⏳"
                lines.append(f"{emoji} {tx.type} {sign}{tx.amount} $ - {tx.created_at.strftime('%d/%m')}")
            text = "\n".join(lines)
        await query.edit_message_text(text, parse_mode='MarkdownV2',
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 الرجوع", callback_data="back")]]))

    elif data == "referral":
        text = f"""🎁 *اربح مكافآت الإحالة*
شارك كودك الخاص مع أصدقائك:
`{user.referral_code}`

رابط الدعوة الخاص بك:
`https://t.me/YourBotName?start={user.referral_code}`

💰 ستحصل على *5%* من قيمة أول شحنة لكل صديق!"""
        await query.edit_message_text(text, parse_mode='MarkdownV2',
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 الرجوع", callback_data="back")]]))

    elif data == "support":
        await query.edit_message_text("📞 *للتواصل مع الدعم الفني*\nمراسلة المطور: @YourSupportUsername",
            parse_mode='MarkdownV2',
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 الرجوع", callback_data="back")]]))

    elif data == "back":
        await query.edit_message_text("📋 *القائمة الرئيسية*", parse_mode='MarkdownV2', reply_markup=get_main_keyboard())

# ---------- معالج محادثة الشحن ----------
async def recharge_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await update.callback_query.edit_message_text(
        "💳 *أدخل المبلغ الذي تريد شحنه (بالدولار):*\n\n"
        "يمكنك إدخال أي قيمة رقمية (مثال: 15.5 أو 100).\n"
        "لإلغاء العملية أرسل /cancel",
        parse_mode='MarkdownV2'
    )
    return AMOUNT

async def recharge_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = float(update.message.text.strip())
        if amount <= 0:
            await update.message.reply_text("⚠️ يرجى إدخال مبلغ أكبر من صفر.")
            return AMOUNT
        context.user_data['recharge_amount'] = amount
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ تأكيد الشحن", callback_data="confirm_deposit"),
             InlineKeyboardButton("❌ إلغاء", callback_data="cancel_deposit")]
        ])
        await update.message.reply_text(
            f"💰 أنت على وشك شحن مبلغ *{amount:.2f} $*.\n"
            "هل تريد المتابعة؟",
            parse_mode='MarkdownV2',
            reply_markup=keyboard
        )
        return CONFIRM
    except ValueError:
        await update.message.reply_text("⚠️ يرجى إدخال رقم صحيح (مثال: 50 أو 12.5).")
        return AMOUNT

async def recharge_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = str(query.from_user.id)
    amount = context.user_data.get('recharge_amount', 0)
    if amount <= 0:
        await query.edit_message_text("حدث خطأ، يرجى المحاولة مجدداً.")
        return ConversationHandler.END

    user = User.query.filter_by(telegram_id=user_id).first()
    if not user:
        await query.edit_message_text("المستخدم غير موجود، يرجى إعادة /start")
        return ConversationHandler.END

    user.balance += amount
    tx = Transaction(
        user_id=user.telegram_id,
        type='deposit',
        amount=amount,
        status='completed',
        description=f"شحن عبر البوت (مبلغ {amount})"
    )
    db.session.add(tx)
    db.session.commit()

    await query.edit_message_text(
        f"✅ *تم الشحن بنجاح!*\nتم إضافة *{amount:.2f} $* إلى رصيدك.\n"
        f"رصيدك الحالي: `{user.balance:.2f} $`",
        parse_mode='MarkdownV2',
        reply_markup=get_main_keyboard()
    )
    context.user_data.clear()
    return ConversationHandler.END

async def recharge_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text("❌ تم إلغاء عملية الشحن.", reply_markup=get_main_keyboard())
    else:
        await update.message.reply_text("❌ تم الإلغاء.", reply_markup=get_main_keyboard())
    context.user_data.clear()
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ تم إلغاء العملية.", reply_markup=get_main_keyboard())
    context.user_data.clear()
    return ConversationHandler.END

# ---------- تشغيل البوت ----------
async def run_bot():
    print("🚀 بدء تشغيل البوت...")
    application = Application.builder().token(BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(recharge_start, pattern="^recharge$")],
        states={
            AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, recharge_amount)],
            CONFIRM: [
                CallbackQueryHandler(recharge_confirm, pattern="^confirm_deposit$"),
                CallbackQueryHandler(recharge_cancel, pattern="^cancel_deposit$")
            ]
        },
        fallbacks=[CommandHandler("cancel", cancel)]
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(conv_handler)

    await application.initialize()
    await application.start()
    await application.updater.start_polling()
    print("✅ البوت يعمل الآن...")

# ---------- تشغيل التطبيق ----------
if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    
    import threading
    import asyncio
    import sys

    def start_bot():
        """تشغيل البوت مع التقاط الأخطاء"""
        try:
            print("🚀 بدء تشغيل البوت من الخيط...")
            asyncio.run(run_bot())
        except Exception as e:
            print(f"❌ فشل تشغيل البوت: {e}")
            import traceback
            traceback.print_exc()

    # تشغيل البوت في خيط منفصل (غير Daemon)
    bot_thread = threading.Thread(target=start_bot, daemon=False)
    bot_thread.start()
    print("✅ تم بدء خيط البوت")

    # تشغيل خادم Flask
    port = int(os.getenv('PORT', 5000))
    print(f"🌐 تشغيل Flask على المنفذ {port}")
    app.run(host='0.0.0.0', port=port, debug=False)
