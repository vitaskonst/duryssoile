import telebot
import requests
from decouple import config


bot = telebot.TeleBot(config('token'))

SERVICE_URL = 'http://duryssoile.nu.edu.kz/api/v1.0'

type_to_description = {
    'parasite': 'Бөгде сөздер',
    'commonly-mispronounced': 'Жиі қате айтылатын сөздер'
}

description_to_type = {
    'Бөгде сөздер': 'parasite',
    'Жиі қате айтылатын сөздер': 'commonly-mispronounced'
}


WORDS_PER_PAGE = 10


bot.set_my_commands([
    telebot.types.BotCommand('/start', 'Басты мәзір'),
    telebot.types.BotCommand('/help', 'Нұсқаулықты көрсету'),
    telebot.types.BotCommand('/search_parasite', 'Бөгде сөздерді іздеу'),
    telebot.types.BotCommand('/search_commonly_mispronounced', 'Жиі қате айтылатын сөздерді іздеу')
])


@bot.message_handler(commands=['start'])
def greeting(message):
    with open('text/greeting.txt', encoding='utf-8') as file:
        greeting = file.readlines()

    markup_reply = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    item_parasite = telebot.types.KeyboardButton(text=type_to_description['parasite'])
    item_commonly_mispronounced = telebot.types.KeyboardButton(text=type_to_description['commonly-mispronounced'])

    markup_reply.add(item_parasite, item_commonly_mispronounced)
    bot.send_message(message.chat.id, greeting, reply_markup=markup_reply)


@bot.message_handler(commands=['help'])
def help(message):
    with open('text/help.txt', encoding='utf-8') as file:
        help_message = file.readlines()

    bot.send_message(message.chat.id, help_message)


def send_word(chat_id, word):
    word_id = word['id']
    word_audio = requests.get(SERVICE_URL + f'/audio/{word_id}', allow_redirects=True)

    bot.send_audio(chat_id, caption=word['word'], audio=word_audio.content, title=word['word'])


def get_list_markup(type, offset, limit, filter):
    payload = {'type': type, 'offset': str(offset), 'limit': str(limit), 'filter': filter}
    r = requests.get(SERVICE_URL + '/words', params=payload)

    if r.status_code != 200:
        print('Error accessing the service')
        return

    words = r.json()

    # generate list of words as inline buttons
    markup_inline = telebot.types.InlineKeyboardMarkup()
    
    for word in words:
        item_word = telebot.types.InlineKeyboardButton(text=word['word'], callback_data=str(word['id']))
        markup_inline.add(item_word)

    # generate arrow buttons
    has_arrow_left = False
    has_arrow_right = False

    if offset > 0:
        callback_data = f'goto:{offset - 1}'
        item_prev = telebot.types.InlineKeyboardButton(text='⬅️', callback_data=callback_data)
        has_arrow_left = True
    
    # generate right arrow if list with greater offset is not empty
    payload = {'type': type, 'offset': str(int(offset) + 1), 'limit': str(limit), 'filter': filter}
    r = requests.get(SERVICE_URL + '/words', params=payload)
    
    if len(r.json()):
        callback_data = f'goto:{offset + 1}'
        item_next = telebot.types.InlineKeyboardButton(text='➡️', callback_data=callback_data)
        has_arrow_right = True

    if has_arrow_left and has_arrow_right:
        markup_inline.add(item_prev, item_next)
    elif has_arrow_left:
        markup_inline.add(item_prev)
    elif has_arrow_right:
        markup_inline.add(item_next)

    return markup_inline


def send_list(chat_id, type, description):
    markup_inline = get_list_markup(type=type, offset=0, limit=WORDS_PER_PAGE, filter='')
    bot.send_message(chat_id, text=description, reply_markup=markup_inline)


@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    if call.data.startswith('goto'):
        cid = call.message.chat.id
        mid = call.message.message_id

        offset = int(call.data.split(':')[1])
        type = description_to_type[call.message.text]

        markup_inline = get_list_markup(type=type, offset=offset, limit=WORDS_PER_PAGE, filter='')
        bot.edit_message_text(chat_id=cid, message_id=mid, text=call.message.text, reply_markup=markup_inline)

    elif call.data.isdigit():
        word_id = int(call.data)
        
        r = requests.get(SERVICE_URL + f'/words/{word_id}', allow_redirects=True)
        if r.status_code == 200:
            send_word(call.message.chat.id, r.json())


@bot.message_handler(commands=['search_parasite'])
def search_parasite(message):
    filter = ' '.join(message.text.split()[1:])
    markup_inline = get_list_markup(type='parasite', offset=0, limit=WORDS_PER_PAGE, filter=filter)
    bot.send_message(message.chat.id, text=type_to_description['parasite'], reply_markup=markup_inline)


@bot.message_handler(commands=['search_commonly_mispronounced'])
def search_parasite(message):
    filter = ' '.join(message.text.split()[1:])
    markup_inline = get_list_markup(type='commonly-mispronounced', offset=0, limit=WORDS_PER_PAGE, filter=filter)
    bot.send_message(message.chat.id, text=type_to_description['commonly-mispronounced'], reply_markup=markup_inline)


@bot.message_handler(content_types=['text'])
def default(message):
    if message.text.strip() in description_to_type:
        description = message.text.strip()
        send_list(message.chat.id, description_to_type[description], description)
        return

    bot.send_message(message.chat.id, 'Белгісіз команда')


if __name__ == '__main__':
    bot.infinity_polling()

# TODO: автовопроизведение