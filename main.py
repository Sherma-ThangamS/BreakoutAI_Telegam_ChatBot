import logging
import os
from telegram import (ReplyKeyboardMarkup, ReplyKeyboardRemove, Update, KeyboardButton, InputFile)
from telegram.ext import (Application, CommandHandler, ContextTypes, MessageHandler, filters)
from pymongo import MongoClient
from pymongo.server_api import ServerApi
import google.generativeai as genai
from google.generativeai.types.safety_types import HarmCategory, HarmBlockThreshold
from datetime import datetime
import PIL.Image as load_image
from io import BytesIO
import requests

# Enable logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                    level=logging.INFO)

logger = logging.getLogger(__name__)

# MongoDB Atlas connection URI
uri = os.environ.get("mongodb_uri", None)
client = MongoClient(uri, server_api=ServerApi('1'))

# Send a ping to confirm a successful connection
try:
    client.admin.command('ping')
    print("Pinged your deployment. You successfully connected to MongoDB!")
except Exception as e:
    print(f"Error connecting to MongoDB: {e}")

# Setup database and collection
db = client['telegram_users']
collection = db['users']
chat_history_collection = db['chat_history']  #  collection for chat history
file_metadata_collection = db['file_metadata']  # collection for file metadata

genai.configure(api_key=os.environ.get("genai_apiKey",None))  

# Disable all safety filters
SAFETY_SETTINGS = {
    HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE,
    HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_NONE,
    HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_NONE,
    HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_NONE,
}

# Function to register a user in MongoDB
def register_user(user_id: int, first_name: str, username: str):
    if not collection.find_one({"chat_id": user_id}):
        user_data = {
            "chat_id": user_id,
            "first_name": first_name,
            "username": username,
            "phone_number": None  # Phone number is initially None
        }
        collection.insert_one(user_data)
        logger.info(f"Registered new user: {first_name} (@{username})")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Starts the conversation, registers the user, and asks for the phone number."""
    user = update.message.from_user
    register_user(user.id, user.first_name, user.username)  # Register the user in MongoDB

    # Request phone number via contact button
    phone_button = KeyboardButton(text="Share my phone number", request_contact=True)
    reply_markup = ReplyKeyboardMarkup([[phone_button]], one_time_keyboard=True, resize_keyboard=True)

    await update.message.reply_text(
        "Welcome to the User Registration Bot! Please share your phone number to continue.",
        reply_markup=reply_markup
    )
    return 0  # End of the conversation step for registration

async def handle_contact(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles the phone number provided by the user."""
    user = update.message.from_user
    phone_number = update.message.contact.phone_number

    # Update the user's phone number in MongoDB
    collection.update_one(
        {"chat_id": user.id},
        {"$set": {"phone_number": phone_number}}
    )

    await update.message.reply_text(f"Thank you for sharing your phone number: {phone_number}. Registration complete!")
    return 0  # End of the conversation step

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancels and ends the conversation."""
    await update.message.reply_text('Bye! Hope to talk to you again soon.', reply_markup=ReplyKeyboardRemove())
    return 0  # End the conversation

# Function to handle user queries and generate Gemini-powered responses
async def gemini_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles user input, sends it to the Gemini API, and stores the conversation in MongoDB."""
    user_input = update.message.text
    user = update.message.from_user
    chat_id = update.message.chat_id

    # Get a response from Gemini API (Google Generative AI)
    try:
        model = genai.GenerativeModel("gemini-pro", safety_settings=SAFETY_SETTINGS)
        chat = model.start_chat(history=[])
        bot_response = chat.send_message(user_input).text

        # Store the chat history in MongoDB
        chat_history = {
            "chat_id": chat_id,
            "user_input": user_input,
            "bot_response": bot_response,
            "timestamp": datetime.now()
        }
        chat_history_collection.insert_one(chat_history)

        # Send the bot response to the user
        await update.message.reply_text(bot_response)

    except Exception as e:
        logger.error(f"Error with Gemini API: {e}")
        await update.message.reply_text("Sorry, I couldn't process your request right now. Please try again later.")

# Function to handle image/file input, process using Gemini, and store metadata
async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles images/files sent by the user, analyzes with Gemini, and stores metadata in MongoDB."""
    file = update.message.document or update.message.photo[-1]  # Handle document or image
    file_id = file.file_id
    file_info = await context.bot.get_file(file_id)
    file_path = file_info.file_path

    # Download the file
    file_name = file.file_name if hasattr(file, 'file_name') else f"image_{file_id}.jpg"
    file_data = load_image.open(BytesIO(await file_info.download_as_bytearray()))
    prompt = "Analyse this image and generate response"
    # Analyze the file with the Gemini vision model
    try:
        img_model = genai.GenerativeModel("gemini-1.5-flash", safety_settings=SAFETY_SETTINGS)
        img_analysis = img_model.generate_content([prompt, file_data])
        description = img_analysis.text
        
        # Store file metadata in MongoDB
        file_metadata = {
            "file_id": file_id,
            "file_name": file_name,
            "description": description,
            "timestamp": datetime.now()
        }
        file_metadata_collection.insert_one(file_metadata)

        # Reply to the user with the description
        await update.message.reply_text(f"Here is what I found in the image:\n\n{description}")

    except Exception as e:
        logger.error(f"Error analyzing file with Gemini: {e}")
        await update.message.reply_text("Sorry, I couldn't analyze the file right now. Please try again later.")

# Define a function to perform a web search using SerpAPI 
def web_search(query: str, chat_id):
    # Replace with your SerpAPI key
    api_key = os.environ.get("serp_api_key",None)
    url = 'https://serpapi.com/search'
    
    # Send a GET request to SerpAPI
    params = {
        'q': query,
        'api_key': api_key
    }
    
    response = requests.get(url, params=params)
    
    if response.status_code == 200:
        results = response.json().get('organic_results', [])
        if not results:
            return "Sorry, I couldn't find relevant results for your query."
        
        # Extract the first result and summarize
        top_result = results[0]
        summary = f"Here is a summary for your query:\n\n"
        summary += f"{top_result.get('snippet', 'No summary available')}...\n\n"
        summary += f"For more details, check the full article here: {top_result.get('link')}"
        
        # Save web search history to MongoDB with chat_id
        websearch_history = {
            "chat_id": chat_id,
            "query": query,
            "summary": summary,
            "timestamp": datetime.now()
        }
        chat_history_collection.insert_one(websearch_history)
        
        return summary
    else:
        return "Sorry, I encountered an error while fetching search results."

# Define a handler for the /websearch command
async def websearch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles web search query and returns summarized results."""
    chat_id = update.message.chat_id
    user_input = ' '.join(context.args)
    if not user_input:
        await update.message.reply_text("Please provide a query to search for. Usage: /websearch <your_query>")
        return

    # Perform the web search
    search_results = web_search(user_input,chat_id)
    
    # Send the results to the user
    await update.message.reply_text(search_results)
    
def main() -> None:
    """Run the bot."""
    application = Application.builder().token(os.environ.get("tele_api_key",None)).build()

    # Register handlers
    application.add_handler(CommandHandler('start', start))
    application.add_handler(MessageHandler(filters.CONTACT, handle_contact))  # Handles phone number input
    application.add_handler(CommandHandler('cancel', cancel))  # Allows user to cancel the registration
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, gemini_query))  # Handles chat queries
    application.add_handler(MessageHandler(filters.PHOTO, handle_file))  # Handles image/file input
    application.add_handler(CommandHandler('websearch', websearch))  # Handles web search command

    application.run_polling()

if __name__ == '__main__':
    main()
