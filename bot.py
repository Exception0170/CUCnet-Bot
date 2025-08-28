import logging
import sqlite3
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters,
    ContextTypes, ConversationHandler, CallbackQueryHandler
)
from config import BOT_TOKEN, ADMIN_CHAT_ID, MAX_PROFILES_PER_USER
from database import init_db
from wireguard import get_next_ip, generate_wireguard_config, add_peer_to_server, generate_keys, remove_peer_from_server
import subprocess
import os

# Set up logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Conversation states
PROFILE_NAME, PROFILE_TYPE = range(2)

# Initialize database
init_db()

# Database connection function for reuse
def get_db_connection():
    return sqlite3.connect('users.db')

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send welcome message and check if user is verified"""
    user = update.effective_user
    user_id = user.id
    username = user.username or user.first_name
    
    # Add user to database if not exists
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('INSERT OR IGNORE INTO users (telegram_id, telegram_username) VALUES (?, ?)', (user_id, username))
    conn.commit()
    
    # Check if user is verified
    c.execute('SELECT is_verified FROM users WHERE telegram_id = ?', (user_id,))
    user_data = c.fetchone()
    conn.close()
    
    if user_data and user_data[0]:
        welcome_text = """
        🤖 Welcome to the CUCnet Management Bot!

        Available commands:
        /profile - Create a new VPN profile
        /profiles - List your profiles
        /delete - Delete a profile by name
        """
        await update.message.reply_text(welcome_text)
    else:
        welcome_text = """
        🤖 Welcome to the CUCnet Management Bot!
        
        You need to be verified to use this bot. 
        Please use /verify to request access.
        """
        await update.message.reply_text(welcome_text)

async def verify_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /verify command - send request to admin"""
    user = update.effective_user
    user_id = user.id
    username = user.username or user.first_name
    
    conn = get_db_connection()
    c = conn.cursor()
    
    # Check if already verified
    c.execute('SELECT is_verified FROM users WHERE telegram_id = ?', (user_id,))
    user_data = c.fetchone()
    
    if user_data and user_data[0]:
        await update.message.reply_text("✅ You are already verified!")
        conn.close()
        return
    
    # Check if pending request exists
    c.execute('SELECT id FROM admin_requests WHERE user_id = (SELECT id FROM users WHERE telegram_id = ?) AND status = "pending"', (user_id,))
    pending_request = c.fetchone()
    
    if pending_request:
        await update.message.reply_text("⏳ You already have a pending verification request. Please wait for admin approval.")
        conn.close()
        return
    
    # Create new request
    c.execute('INSERT INTO admin_requests (user_id) VALUES ((SELECT id FROM users WHERE telegram_id = ?))', (user_id,))
    conn.commit()
    conn.close()
    
    # Send request to admin
    keyboard = [
        [
            InlineKeyboardButton("✅ Approve", callback_data=f"approve_{user_id}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"reject_{user_id}"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    admin_message = f"🔔 New Verification Request\n\nFrom: @{username}\nUser ID: {user_id}"
    await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=admin_message, reply_markup=reply_markup)
    
    await update.message.reply_text("✅ Verification request sent to admin. You will be notified of the decision.")

async def handle_verification_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle approve/reject callback from admin"""
    query = update.callback_query
    await query.answer()
    
    data = query.data
    action, user_id = data.split('_', 1)
    user_id = int(user_id)
    
    conn = get_db_connection()
    c = conn.cursor()
    
    # Get user info
    c.execute('SELECT telegram_id, telegram_username FROM users WHERE telegram_id = ?', (user_id,))
    user_data = c.fetchone()
    
    if not user_data:
        await query.edit_message_text("❌ User not found.")
        conn.close()
        return
    
    telegram_id, username = user_data
    
    if action == 'approve':
        # Update user verification status
        c.execute('UPDATE users SET is_verified = 1 WHERE telegram_id = ?', (user_id,))
        c.execute('UPDATE admin_requests SET status = "approved" WHERE user_id = (SELECT id FROM users WHERE telegram_id = ?)', (user_id,))
        conn.commit()
        
        # Notify user
        await context.bot.send_message(chat_id=user_id, text="🎉 Your verification request has been approved! You can now use the bot commands.")
        await query.edit_message_text(f"✅ Approved verification for @{username}")
        
    elif action == 'reject':
        # Update request status
        c.execute('UPDATE admin_requests SET status = "rejected" WHERE user_id = (SELECT id FROM users WHERE telegram_id = ?)', (user_id,))
        conn.commit()
        
        # Notify user
        await context.bot.send_message(chat_id=user_id, text="❌ Your verification request has been rejected.")
        await query.edit_message_text(f"❌ Rejected verification for @{username}")
    
    conn.close()

async def profile_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start the profile creation conversation"""
    user = update.effective_user
    
    # Check if user is verified
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('SELECT is_verified FROM users WHERE telegram_id = ?', (user.id,))
    user_data = c.fetchone()
    conn.close()
    
    if not user_data or not user_data[0]:
        await update.message.reply_text("❌ You need to be verified to use this command. Use /verify first.")
        return ConversationHandler.END
    
    # Check profile limit
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('SELECT COUNT(*) FROM profiles WHERE user_id = (SELECT id FROM users WHERE telegram_id = ?)', (user.id,))
    profile_count = c.fetchone()[0]
    conn.close()
    
    if profile_count >= MAX_PROFILES_PER_USER:
        await update.message.reply_text(f"❌ You have reached the maximum limit of {MAX_PROFILES_PER_USER} profiles.")
        return ConversationHandler.END
    
    await update.message.reply_text("Please enter a name for your new profile:")
    return PROFILE_NAME

async def handle_profile_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle profile name input"""
    profile_name = update.message.text.strip()
    user = update.effective_user
    
    # Store profile name in context
    context.user_data['profile_name'] = f"{user.username}-{profile_name}" if user.username else f"{user.id}-{profile_name}"
    
    # Ask for profile type
    keyboard = [
        [InlineKeyboardButton("Personal", callback_data='personal')],
        [InlineKeyboardButton("Website", callback_data='website')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text("What type of profile is this?", reply_markup=reply_markup)
    return PROFILE_TYPE

async def handle_profile_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle profile type selection and create the profile"""
    query = update.callback_query
    await query.answer()
    
    profile_type = query.data
    user = query.from_user
    profile_name = context.user_data['profile_name']
    
    # Generate keys and IP
    try:
        # Generate WireGuard keys
        private_key, public_key = generate_keys()
        if not private_key or not public_key:
            await query.edit_message_text("❌ Failed to generate keys. Please contact admin.")
            return ConversationHandler.END
        
        # Get next available IP
        ip_address = get_next_ip(profile_type)
        if not ip_address:
            await query.edit_message_text("❌ No available IP addresses in the range. Please contact admin.")
            return ConversationHandler.END
        
        # Add peer to server
        if not add_peer_to_server(public_key, ip_address, profile_name):
            await query.edit_message_text("❌ Failed to add profile to server. Please contact admin.")
            return ConversationHandler.END
        
        # Save to database
        conn = get_db_connection()
        c = conn.cursor()
        c.execute(
            'INSERT INTO profiles (user_id, profile_name, profile_type, wg_public_key, wg_private_key, wg_ip_address) '
            'VALUES ((SELECT id FROM users WHERE telegram_id = ?), ?, ?, ?, ?, ?)',
            (user.id, profile_name, profile_type, public_key, private_key, ip_address)
        )
        conn.commit()
        conn.close()
        
        # Generate config file
        config_content = generate_wireguard_config(profile_name, profile_type, private_key, ip_address)
        
        # Send config to user
        await context.bot.send_document(
            chat_id=user.id,
            document=config_content.encode('utf-8'),
            filename=f"{profile_name}.conf",
            caption=f"✅ Profile '{profile_name}' created successfully!\nIP: {ip_address}\nType: {profile_type}"
        )
        
        await query.edit_message_text(f"✅ Profile created successfully! Check your messages for the config file.")
        
    except Exception as e:
        logger.error(f"Error creating profile: {e}")
        await query.edit_message_text("❌ An error occurred while creating the profile. Please contact admin.")
    
    return ConversationHandler.END

async def list_profiles(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List all profiles for the user"""
    user = update.effective_user
    
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        'SELECT profile_name, profile_type, wg_ip_address FROM profiles '
        'WHERE user_id = (SELECT id FROM users WHERE telegram_id = ?) AND is_active = 1',
        (user.id,)
    )
    profiles = c.fetchall()
    conn.close()
    
    if not profiles:
        await update.message.reply_text("You don't have any profiles yet. Use /profile to create one.")
        return
    
    message = "Your profiles:\n\n"
    for profile in profiles:
        # Extract just the profile name part (remove username- prefix)
        display_name = profile[0].split('-', 1)[1] if '-' in profile[0] else profile[0]
        message += f"• {display_name} ({profile[1]}) - {profile[2]}\n"
    
    await update.message.reply_text(message)

async def delete_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Delete a profile by name"""
    user = update.effective_user
    
    # Check if user is verified
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('SELECT is_verified FROM users WHERE telegram_id = ?', (user.id,))
    user_data = c.fetchone()
    
    if not user_data or not user_data[0]:
        await update.message.reply_text("❌ You need to be verified to use this command. Use /verify first.")
        conn.close()
        return
    
    if not context.args:
        await update.message.reply_text("Usage: /delete <profile_name>\n\nUse /profiles to see your profile names.")
        conn.close()
        return
    
    profile_name_to_delete = context.args[0]
    full_profile_name = f"{user.username}-{profile_name_to_delete}" if user.username else f"{user.id}-{profile_name_to_delete}"
    
    # Get profile info
    c.execute(
        'SELECT id, wg_public_key, profile_name FROM profiles '
        'WHERE profile_name = ? AND user_id = (SELECT id FROM users WHERE telegram_id = ?) AND is_active = 1',
        (full_profile_name, user.id)
    )
    profile = c.fetchone()
    
    if not profile:
        await update.message.reply_text("❌ Profile not found or already deleted.")
        conn.close()
        return
    
    profile_id, public_key, full_profile_name = profile
    
    # Remove from server
    try:
        success = remove_peer_from_server(public_key)
        
        if success:
            # Mark as inactive in database
            c.execute('UPDATE profiles SET is_active = 0 WHERE id = ?', (profile_id,))
            conn.commit()
            
            await update.message.reply_text(f"✅ Profile '{profile_name_to_delete}' deleted successfully.")
        else:
            await update.message.reply_text("❌ Failed to delete profile from server. Please contact admin.")
        
    except Exception as e:
        logger.error(f"Failed to delete profile: {e}")
        await update.message.reply_text("❌ An error occurred while deleting the profile. Please contact admin.")
    
    conn.close()

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel the conversation"""
    await update.message.reply_text('Operation cancelled.')
    return ConversationHandler.END

def main():
    """Start the bot"""
    application = Application.builder().token(BOT_TOKEN).build()
    
    # Add handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("verify", verify_command))
    application.add_handler(CommandHandler("profiles", list_profiles))
    application.add_handler(CommandHandler("delete", delete_profile))
    application.add_handler(CallbackQueryHandler(handle_verification_callback, pattern=r'^(approve|reject)_'))
    
    # Conversation handler for profile creation
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('profile', profile_command)],
        states={
            PROFILE_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_profile_name)],
            PROFILE_TYPE: [CallbackQueryHandler(handle_profile_type, pattern=r'^(personal|website)$')],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
    )
    application.add_handler(conv_handler)
    #notify
    if ADMIN_CHAT_ID:
        try:
            # Create a dedicated Bot instance to send this initial message.
            # This is done synchronously before the application starts its polling loop.
            startup_bot = Bot(token=BOT_TOKEN)
            startup_bot.send_message(chat_id=ADMIN_CHAT_ID, text="Bot started successfully! 🚀")
            logger.info(f"Startup message sent to admin chat ID: {ADMIN_CHAT_ID}")
        except Exception as e:
            logger.error(f"Failed to send startup message to admin chat ID {ADMIN_CHAT_ID}: {e}")
    # Start the bot
    logger.info("Bot is starting...")
    application.run_polling()

if __name__ == '__main__':
    main()