
from random import random, shuffle, choice, randint

import flask
import os
import uuid
import pydub

from Game import Game
from Request import Request
from Response import Response


app = flask.Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', uuid.uuid4().hex)

active_games = []

def get_game_state_response(game_id):
    game = next((g for g in active_games if g.id == game_id), None)
    if game is None:
        return None
    response = Response(
        game_id=game.id,
        current_bonus_points=game.bonus_points,
        current_round=game.round_number,
        shield_left=game.shield_left,
        lives_left=game.lives,
        current_song=game.current_song,
        is_infinite=game.is_infinite,
        game_ended=game.game_ended
    )
    return response

@app.route('/autofill', methods=['GET'])
def autofill():
    return flask.jsonify({'games': os.listdir('assets/games')}), 200

@app.route('/game_state/<game_id>', methods=['GET'])
def game_state(game_id):
    game = next((g for g in active_games if g.id == game_id), None)
    if game is None:
        return flask.jsonify({'error': 'Game not found'}), 404
    response = get_game_state_response(game_id)
    return flask.jsonify(response.__dict__), 200

@app.route('/get_audio/<game_id>/<song_number>', methods=['GET'])
def get_audio(game_id, song_number):
    game = next((g for g in active_games if g.id == game_id), None)
    if game is None:
        return flask.jsonify({'error': 'Game not found'}), 404
    if game.current_song < int(song_number):
        return flask.jsonify({'error': 'Song not unlocked'}), 403

    song_path = f"temp/{game_id}/{song_number}.mp3"
    if not os.path.exists(song_path):
        return flask.jsonify({'error': 'Audio file not found'}), 404

    return flask.send_file(song_path, mimetype='audio/mpeg')

@app.route('/play', methods=['GET'])
def play():
    request = Request(**flask.request.json)
    game = None
    if request.game_id is not None:
        game = next((g for g in active_games if g.id == request.game_id), None)

    if game and game.round_completed and request.action != 'next':
        return flask.jsonify({'error': 'Round completed, please go to next round'}), 400
    
    if game and game.game_ended:
        response = get_game_state_response(game.id)
        response.response_text = "Game has ended. Please start a new game."
        return flask.jsonify(response.__dict__), 200
    
    if request.action == 'guess':
        correct = request.guessed_game.lower() == game.current_game.lower()
        if correct:
            if game.current_song == 0 and game.is_infinite:
                game.bonus_points += 1
            game.round_completed = True
            response = get_game_state_response(game.id)
            response.is_correct = True
            response.response_text = f"Correct , the game was {game.current_game}"
            return flask.jsonify(response.__dict__), 200
        else:
            if game.shield_left > 0:
                game.shield_left -= 1
            else:
                game.lives -= 1
                if game.lives <= 0:
                    response = get_game_state_response(game.id)
                    response.response_text = "Game over"
                    return flask.jsonify(response.__dict__), 200
                
                if game.current_song == len(game.song_order) - 1: #new round
                    game.round_completed = True
                else:
                    game.current_song += 1
            response = get_game_state_response(game.id)
            response.response_text = "Incorrect"
            return flask.jsonify(response.__dict__), 200
    elif request.action == 'next':
        if not game.round_completed:
            return flask.jsonify({'error': 'Round not completed'}), 400
        next_round = go_to_next_round(game)
        if next_round is None:
            response_text = f"Congratulations! You won, the game was {game.current_game}"
        else:
            response_text = "Next round"
        response = get_game_state_response(game.id)
        response.response_text = response_text
        return flask.jsonify(response.__dict__), 200
    elif request.action == 'ability':
        if game.bonus_points < 1:
            return flask.jsonify({'error': 'Not enough bonus points'}), 400
        if request.ability_used == 'shield':
            game.shield_left = 3
            game.bonus_points -= 1
            response_text = "Shield activated"
        elif request.ability_used == 'unlock':
            game.current_song = 2 #unlock all songs
            game.bonus_points -= 1
            game.all_unlocked = True
            response_text = "All songs unlocked"
        elif request.ability_used == 'skip_round':
            game.round_completed = True
            game.current_song = 2
            game.bonus_points -= 1
            response_text=f"Round skipped, game was {game.current_game}",
        elif request.ability_used == 'extra_life':
            game.lives += 1
            game.bonus_points -= 1
            response_text = "Extra life granted"
        else:
            return flask.jsonify({'error': 'Invalid ability'}), 400
        response = get_game_state_response(game.id)
        response.response_text = response_text
        return flask.jsonify(response.__dict__), 200
    elif request.action == 'skip':
        game.lives -= 1
        if game.lives <= 0:
            return flask.jsonify({'response_text': f"Game over on round {game.round_number}, game was {game.current_game}."}), 200
        response_text = ""
        if game.current_song == len(game.song_order) - 1: #new round
            game.round_completed = True
            response_text = f"Skipped to next round, game was {game.current_game}"
        else:
            game.current_song += 1
            response_text = f"Skipped to next song"
        response = get_game_state_response(game.id)
        response.response_text = response_text
        return flask.jsonify(response.__dict__), 200
    elif request.action == 'start':
        new_game = start_game(request.infinite)
        response = get_game_state_response(new_game.id)
        response.response_text = "New game started"
        return flask.jsonify(response.__dict__), 200
    else:
        return flask.jsonify({'error': 'Invalid action'}), 400

def go_to_next_round(game):
    game.round_number += 1
    game.previous_games.append(game.current_game)
    if game.is_infinite:
        game.shield_left = 0
        game.all_unlocked = False
    new_random_game = get_random_game(game.previous_games)
    if new_random_game is None:
        return None #player played all games
    game.current_song = 0
    game.current_game = new_random_game[0]
    game.song_order = new_random_game[1]
    game.round_completed = False
    prepare_audio_for_round(game.id, game.current_game, game.song_order)
    return game


def get_random_game(past_games):
    all_games = os.listdir('assets/games')
    available_games = [g for g in all_games if g not in past_games]
    if len(available_games) == 0:
        return None #player played all games
    chosen_game = choice(available_games)
    songs = os.listdir(f'assets/games/{chosen_game}')
    shuffle(songs)
    songs = songs[:3] #take first three songs
    return chosen_game, songs
    

def start_game(is_infinite = False):
    random_game = get_random_game([])
    id = str(uuid.uuid4())
    prepare_audio_for_round(id, random_game[0], random_game[1])
    new_game = Game(
        id=id,
        round_number=1,
        previous_games=[],
        is_infinite=is_infinite,
        lives=5,
        bonus_points=0,
        current_game=random_game[0],
        shield_left=0,
        all_unlocked=False,
        current_song=0,
        song_order=random_game[1],
        round_completed=False,
        game_ended=False
    )
    active_games.append(new_game)
    return new_game

def prepare_audio_for_round(game_id, game_name, song_order): #puts three 20-second song clips into temp/game_id/0.mp3, temp/game_id/1.mp3, temp/game_id/2.mp3
    src_folder = f"assets/games/{game_name}"
    for i, song in enumerate(song_order):
        src_path = os.path.join(src_folder, song)
        dest_folder = f"temp/{game_id}"
        os.makedirs(dest_folder, exist_ok=True)
        dest_path = os.path.join(dest_folder, f"{i}.mp3")
        audio = pydub.AudioSegment.from_file(src_path)
        max_start = len(audio) - 20000
        start = randint(0, max_start)

        clip = audio[start:start + 20000]
        clip.export(dest_path, format="mp3")

def cleanup_temp_files():#todo
    pass


app.run('0.0.0.0', port=2137, debug=True)