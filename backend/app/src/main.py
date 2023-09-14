import os
import json
from enum import Enum
from pathlib import Path
from copy import deepcopy
from collections import OrderedDict

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse


def extracted(key_to_extract, original_dict):
        returned_dict = deepcopy(original_dict)
        extracted_value = returned_dict.pop(key_to_extract)
        return extracted_value, returned_dict

def load_json(filename):
        with open(filename, mode='r', encoding='utf-8') as file:
                return json.load(file)

def add_word_type(words, word_type, filename):
        for word in load_json(filename):
                word_id, word_info = extracted('id', word)
                word_info |= {'type': word_type}
                words.update({word_id: word_info})


words = {}
add_word_type(words, 'parasite', 'parasite.json')
add_word_type(words, 'commonly-mispronounced', 'commonly-mispronounced.json')


app = FastAPI(root_path='/api/v1.0')

class WordType(str, Enum):
        parasite = 'parasite'
        commonly_mispronounced = 'commonly-mispronounced'

class SortingOrder(str, Enum):
        ascending = 'asc'
        descending = 'desc'


def validate_pagination_parameters(offset, limit):
        details = []

        if offset < 0:
                details.append({'loc': ['query','offset'], 'msg': 'value is not in a valid range; valid range: non-negative numbers', 'type': 'value_error'})

        if limit < 0:
                details.append({'loc': ['query','limit'], 'msg': 'value is not in a valid range; valid range: non-negative numbers','type': 'value_error'})

        if details:
                raise HTTPException(status_code=400, detail=details)


@app.get('/words')
def get_words(type: WordType, filter: str = '', offset: int = 0, limit: int = 20, sort: SortingOrder = SortingOrder.ascending):
        validate_pagination_parameters(offset, limit)

        # filter by word type
        result_ids = [word_id for word_id in words if words[word_id]['type'] == type]

        # filter by 'filter' argument value
        result_ids = [word_id for word_id in result_ids if words[word_id]['word'].lower().startswith(filter.lower())]

        # ordering depending on 'sort' argument value
        if sort == SortingOrder.descending:
                result_ids.reverse()

        # applying pagination
        result_ids = result_ids[offset * limit: (offset + 1) * limit]

        # formatting response (adding 'id' field, removing 'filename', creating array of objects)
        results = [{'id': word_id} | {x: y for x, y in words[word_id].items() if x != 'filename'} for word_id in result_ids]

        return results

@app.get('/words/{word_id}')
def get_word(word_id: int):
        if word_id not in range(len(words)):
                raise HTTPException(status_code=404, detail='Not Found')

        # formatting response (adding 'id' field, removing 'filename')
        results = {'id': word_id} | {x: y for x, y in words[word_id].items() if x != 'filename'}

        return results

@app.get('/audio/{word_id}')
def get_audio(word_id: int):
        if word_id not in range(len(words)):
                raise HTTPException(status_code=404, detail='Not Found')

        path = Path(os.getenv('AUDIO_PATH')) / Path(words[word_id]['type']) / Path(words[word_id]['filename'])
        return FileResponse(path)
