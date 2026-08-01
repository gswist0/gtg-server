import dataclasses
import os
import shutil
import uuid
from random import choice, randint, shuffle

import flask
import pydub
from werkzeug.exceptions import HTTPException

from Game import Game
from Request import Request
from Response import Response


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ASSETS_DIR = os.path.join(BASE_DIR, 'assets', 'games')
TEMP_DIR = os.path.join(BASE_DIR, 'temp')

AUDIO_EXTENSIONS = ('.mp3', '.wav', '.ogg', '.flac', '.m4a')
MAX_LIVES = 5
SONGS_PER_ROUND = 3
CLIP_LENGTH_MS = 20000
SHIELD_CHARGES = 3
MAX_ACTIVE_GAMES = 50
AUDIO_CACHE_SECONDS = 3600

app = flask.Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', uuid.uuid4().hex)

ALLOWED_ORIGIN = os.environ.get('CORS_ALLOWED_ORIGIN', '*')

active_games = []


class NoGamesAvailable(Exception):
    pass


@app.after_request
def add_cors_headers(response):
    response.headers['Access-Control-Allow-Origin'] = ALLOWED_ORIGIN
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    return response


@app.errorhandler(HTTPException)
def handle_http_exception(error):
    return flask.jsonify({'error': error.description}), error.code


@app.errorhandler(Exception)
def handle_unexpected_exception(error):
    app.logger.exception('Unhandled error while serving a request')
    return flask.jsonify({'error': 'Internal server error'}), 500


def find_game(game_id):
    if not game_id:
        return None
    return next((g for g in active_games if g.id == game_id), None)


def build_response(game, response_text="", is_correct=None):
    reveal_answer = game.round_completed or game.game_ended
    return Response(
        game_id=game.id,
        response_text=response_text,
        is_correct=is_correct,
        current_bonus_points=game.bonus_points,
        current_points=game.points,
        current_round=game.round_number,
        current_song=game.current_song,
        total_songs=len(game.song_order),
        lives_left=game.lives,
        max_lives=MAX_LIVES,
        shield_left=game.shield_left,
        all_unlocked=game.all_unlocked,
        round_completed=game.round_completed,
        is_infinite=game.is_infinite,
        game_ended=game.game_ended,
        correct_answer=game.current_game if reveal_answer else None,
    )


def json_state(game, response_text="", is_correct=None, status=200):
    return flask.jsonify(dataclasses.asdict(build_response(game, response_text, is_correct))), status


def json_error(message, status):
    return flask.jsonify({'error': message}), status


def list_available_games():
    if not os.path.isdir(ASSETS_DIR):
        return []
    return sorted(
        entry for entry in os.listdir(ASSETS_DIR)
        if os.path.isdir(os.path.join(ASSETS_DIR, entry))
    )


@app.route('/autofill', methods=['GET'])
def autofill():
    return flask.jsonify({'games': list_available_games()}), 200


@app.route('/game_state/<game_id>', methods=['GET'])
def game_state(game_id):
    game = find_game(game_id)
    if game is None:
        return json_error('Game not found', 404)
    return json_state(game)


@app.route('/get_audio/<game_id>/<song_number>', methods=['GET'])
def get_audio(game_id, song_number):
    game = find_game(game_id)
    if game is None:
        return json_error('Game not found', 404)

    try:
        song_index = int(song_number)
    except ValueError:
        return json_error('Invalid song number', 400)

    if song_index < 0 or song_index >= len(game.song_order):
        return json_error('Invalid song number', 400)

    unlocked = game.all_unlocked or game.round_completed or song_index <= game.current_song
    if not unlocked:
        return json_error('Song not unlocked', 403)

    song_path = os.path.join(TEMP_DIR, game_id, f"{song_index}.mp3")
    if not os.path.exists(song_path):
        return json_error('Audio file not found', 404)

    response = flask.send_file(song_path, mimetype='audio/mpeg')
    #game ids are uuids and the URL carries ?round=, so a clip URL always maps to the same audio.
    #letting the browser keep it is what makes the client-side prefetch and re-listening free.
    response.headers['Cache-Control'] = f'private, max-age={AUDIO_CACHE_SECONDS}, immutable'
    return response


@app.route('/play', methods=['POST'])
def play():
    payload = flask.request.get_json(silent=True) or {}
    known_fields = {f.name for f in dataclasses.fields(Request)}
    request = Request(**{k: v for k, v in payload.items() if k in known_fields})

    if request.action == 'start':
        try:
            new_game = start_game(bool(request.infinite))
        except NoGamesAvailable:
            return json_error(f'No games found in {ASSETS_DIR}', 503)
        return json_state(new_game, "New game started")

    game = find_game(request.game_id)
    if game is None:
        return json_error('Game not found', 404)

    if game.game_ended:
        return json_state(game, "Game has ended. Please start a new game.")

    if game.round_completed and request.action != 'next':
        return json_error('Round completed, please go to next round', 400)

    if request.action == 'guess':
        return handle_guess(game, request)
    elif request.action == 'next':
        return handle_next(game)
    elif request.action == 'ability':
        return handle_ability(game, request)
    elif request.action == 'skip':
        return handle_skip(game)
    else:
        return json_error('Invalid action', 400)


def handle_guess(game, request):
    guess = (request.guessed_game or '').strip()
    if not guess:
        return json_error('No guess provided', 400)

    if guess.lower() == game.current_game.lower():
        if game.current_song == 0 and not game.is_infinite:
            game.bonus_points += 1
        game.points += 1
        game.round_completed = True
        return json_state(game, f"Correct, the game was {game.current_game}", is_correct=True)

    if game.shield_left > 0:
        game.shield_left -= 1
        return json_state(game, "Shield blocked the wrong guess", is_correct=False)

    if not game.is_infinite:
        game.lives -= 1
        if game.lives <= 0:
            game.lives = 0
            game.game_ended = True
            return json_state(
                game,
                f"Game over on round {game.round_number}, the game was {game.current_game}",
                is_correct=False,
            )

    advance_song(game)
    return json_state(game, "Incorrect", is_correct=False)


def handle_next(game):
    if not game.round_completed:
        return json_error('Round not completed', 400)
    if go_to_next_round(game) is None: #player exhausted every game in the library
        game.game_ended = True
        return json_state(game, "Congratulations! You have played through every available game.")
    return json_state(game, "Next round")


def handle_ability(game, request):
    if game.is_infinite:
        return json_error('Abilities are not available in endless mode', 400)
    if game.bonus_points < 1:
        return json_error('Not enough bonus points', 400)

    ability = request.ability_used
    if ability == 'shield':
        if game.shield_left > 0:
            return json_error('Shield is already active', 400)
        game.shield_left = SHIELD_CHARGES
        response_text = "Shield activated"
    elif ability == 'unlock':
        if game.all_unlocked:
            return json_error('Songs are already unlocked', 400)
        game.all_unlocked = True
        game.current_song = len(game.song_order) - 1
        response_text = "All songs unlocked"
    elif ability == 'skip_round':
        game.round_completed = True
        game.current_song = len(game.song_order) - 1
        response_text = f"Round skipped, the game was {game.current_game}"
    elif ability == 'extra_life':
        if game.lives >= MAX_LIVES:
            return json_error('Lives are already full', 400)
        game.lives += 1
        response_text = "Extra life granted"
    else:
        return json_error('Invalid ability', 400)

    game.bonus_points -= 1
    return json_state(game, response_text)


def handle_skip(game):
    if not game.is_infinite:
        game.lives -= 1
        if game.lives <= 0:
            game.lives = 0
            game.game_ended = True
            return json_state(
                game,
                f"Game over on round {game.round_number}, the game was {game.current_game}",
            )

    if game.current_song >= len(game.song_order) - 1: #no songs left, round is over
        game.round_completed = True
        response_text = f"Skipped to next round, the game was {game.current_game}"
    else:
        game.current_song += 1
        response_text = "Skipped to next song"
    return json_state(game, response_text)


def advance_song(game):
    if game.current_song >= len(game.song_order) - 1: #no songs left, round is over
        game.round_completed = True
    else:
        game.current_song += 1


def go_to_next_round(game):
    game.round_number += 1
    game.previous_games.append(game.current_game)
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
    available_games = [g for g in list_available_games() if g not in past_games]
    if len(available_games) == 0:
        return None #player played all games
    chosen_game = choice(available_games)
    songs = [
        s for s in os.listdir(os.path.join(ASSETS_DIR, chosen_game))
        if s.lower().endswith(AUDIO_EXTENSIONS)
    ]
    shuffle(songs)
    songs = songs[:SONGS_PER_ROUND]
    return chosen_game, songs


def start_game(is_infinite=False):
    random_game = get_random_game([])
    if random_game is None or len(random_game[1]) == 0:
        raise NoGamesAvailable()

    cleanup_temp_files()
    id = str(uuid.uuid4())
    prepare_audio_for_round(id, random_game[0], random_game[1])
    new_game = Game(
        id=id,
        round_number=1,
        previous_games=[],
        is_infinite=is_infinite,
        lives=MAX_LIVES,
        bonus_points=0,
        points=0,
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
    src_folder = os.path.join(ASSETS_DIR, game_name)
    dest_folder = os.path.join(TEMP_DIR, game_id)
    os.makedirs(dest_folder, exist_ok=True)
    for i, song in enumerate(song_order):
        src_path = os.path.join(src_folder, song)
        dest_path = os.path.join(dest_folder, f"{i}.mp3")
        audio = pydub.AudioSegment.from_file(src_path)
        max_start = len(audio) - CLIP_LENGTH_MS
        start = randint(0, max_start) if max_start > 0 else 0 #songs shorter than the clip start at 0

        clip = audio[start:start + CLIP_LENGTH_MS]
        clip.export(dest_path, format="mp3")


def cleanup_temp_files():
    #drop the oldest finished games so temp/ does not grow without bound
    while len(active_games) >= MAX_ACTIVE_GAMES:
        stale = next((g for g in active_games if g.game_ended), active_games[0])
        active_games.remove(stale)
        shutil.rmtree(os.path.join(TEMP_DIR, stale.id), ignore_errors=True)


if __name__ == '__main__':
    if not list_available_games():
        print(f"WARNING: no games found in {ASSETS_DIR} - /play will return 503 until you add some.")
    app.run('0.0.0.0', port=2137, debug=True)
