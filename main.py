import dataclasses
import datetime
import os
import shutil
import threading
import uuid
import logging
from random import choice, randint, shuffle
import json


import flask
import pydub
from werkzeug.exceptions import HTTPException

from Game import Game
from Request import Request
from Response import Response

file_path = f"logs/output_{datetime.date.today()}.log"

if not os.path.exists(file_path):
    open(file_path, "x").close()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(file_path)
    ]
)


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
STAGING_SUBDIR = 'next' #temp/<game_id>/next/, where the round after this one is cut


app = flask.Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', uuid.uuid4().hex)

ALLOWED_ORIGIN = os.environ.get('CORS_ALLOWED_ORIGIN', '*')

active_games = []

#cutting a round costs seconds of ffmpeg, so it runs while the player is still
#guessing the current one. game_id -> {'done': Event, 'round': (name, songs) | None}
staged_rounds = {}
next_clip_times = []
staging_lock = threading.Lock()

LIBRARY_EXHAUSTED = object() #staging looked and there is no unplayed game left

abilities_dict = json.loads(open("abilities.json").read())

class NoGamesAvailable(Exception):
    pass

def check_franchise(guessed_game, current_game):
    with open(os.path.join(ASSETS_DIR,"games.json"), "r") as f:
        games_data = json.load(f)
        games_data = {key: [v.lower() for v in values] for key, values in games_data.items()}
        return any(guessed_game.lower() in value and current_game.lower() in value for value in games_data.values())


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
        correct_franchise=game.correct_franchise,
        ability_cooldowns=game.ability_cooldowns,
        clip_times = game.clip_times
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

class NoHealthcheckFilter(logging.Filter):
    def filter(self, record):
        return '/healthcheck' not in record.getMessage()

logging.getLogger('werkzeug').addFilter(NoHealthcheckFilter())

@app.route('/game_history/<game_id>', methods=['GET'])
def game_history(game_id):
    game = find_game(game_id)
    if game is None:
        return json_error('Game not found', 404)
    return flask.jsonify(game.previous_rounds)

@app.route('/abilities_data', methods=['GET'])
def abilities_data():
    return flask.jsonify(abilities_dict), 200

@app.route('/healthcheck', methods=['GET'])
def healthcheck():
    return flask.jsonify({'status': 'ok'}), 200
    
@app.route('/autofill', methods=['GET'])
def autofill():
    return flask.jsonify({'games': list_available_games()}), 200


@app.route('/game_state/<game_id>', methods=['GET'])
def game_state(game_id):
    game = find_game(game_id)
    if game is None:
        return json_error('Game not found', 404)
    return json_state(game)

@app.route('/get_full_audio/<game_id>/<song_number>', methods=['GET'])
def get_full_audio(game_id, song_number):
    logging.info(f"Game {game_id} requested full audio for song {song_number}")
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

    song_path = os.path.join(ASSETS_DIR, game.current_game, game.song_order[song_index])
    if not os.path.exists(song_path):
        return json_error('Audio file not found', 404)

    response = flask.send_file(song_path, mimetype='audio/mpeg')
    response.headers['Cache-Control'] = f'private, max-age={AUDIO_CACHE_SECONDS}, immutable'
    return response


@app.route('/get_audio/<game_id>/<song_number>', methods=['GET'])
def get_audio(game_id, song_number):
    logging.info(f"Game {game_id} requested audio for song {song_number}")
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

    game.last_interaction_date = int(datetime.datetime.now().timestamp())
    game.correct_franchise = None

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
    logging.info(f"Game {game.id} guessed {request.guessed_game}")
    guess = (request.guessed_game or '').strip()
    if not guess:
        return json_error('No guess provided', 400)

    if guess.lower() == game.current_game.lower():
        #unlock no longer moves current_song, so keep it from handing out the
        #first song bonus that unlocking used to rule out
        if game.current_song == 0 and not game.is_infinite and not game.all_unlocked:
            game.bonus_points += 1
        if not game.is_infinite and game.shield_left > 0 and game.lives < MAX_LIVES:
            game.lives += 1

        game.points += 1
        game.round_completed = True
        logging.info(type(game.round))
        game.round["guessed_correctly"] = True
        game.round["guessed_on"] = game.current_song
        save_active_games()
        return json_state(game, f"Correct, the game was {game.current_game}", is_correct=True)

    game.correct_franchise = check_franchise(guess, game.current_game)

    if game.shield_left > 0:
        game.shield_left -= 1
        save_active_games()
        return json_state(game, "Shield blocked the wrong guess", is_correct=False)

    if not game.is_infinite:
        game.lives -= 1
        if game.lives <= 0:
            game.lives = 0
            game.game_ended = True
            save_active_games()
            return json_state(
                game,
                f"Game over on round {game.round_number}, the game was {game.current_game}",
                is_correct=False,
            )

    advance_song(game)
    save_active_games()  # Save the game state after a wrong guess
    return json_state(game, "Incorrect", is_correct=False)


def handle_next(game):
    logging.info(f"Game {game.id} went to next round")
    if not game.round_completed:
        return json_error('Round not completed', 400)
    if go_to_next_round(game) is None: #player exhausted every game in the library
        game.game_ended = True
        save_active_games()
        return json_state(game, "Congratulations! You have played through every available game.")
    save_active_games()  # Save the game state after advancing to the next round
    return json_state(game, "Next round")


def handle_ability(game, request):
    logging.info(f"Game {game.id} used ability {request.ability_used}")
    if game.is_infinite:
        return json_error('Abilities are not available in endless mode', 400)
    

    ability = request.ability_used
    ability_data = abilities_dict.get(ability)
    if not ability_data:
        return json_error('Invalid ability', 400)
    if game.bonus_points < ability_data['cost']:
        return json_error('Not enough bonus points', 400)
    if game.ability_cooldowns.get(ability, 0) > 0:
        return json_error(f"{ability_data['pretty_name']} is on cooldown", 400)
    up = game.round["used_powerups"]
    up.append(ability)
    game.round["used_powerups"] = up
    if ability == 'shield':
        if game.shield_left > 0:
            return json_error('Shield is already active', 400)
        game.shield_left = SHIELD_CHARGES
        game.bonus_points -= ability_data['cost']
        response_text = "Shield activated"
    elif ability == 'unlock':
        if game.all_unlocked:
            return json_error('Songs are already unlocked', 400)
        #current_song doubles as the attempt counter, so it must not move here.
        #the audio routes already serve every song once all_unlocked is set.
        game.all_unlocked = True
        game.bonus_points -= ability_data['cost']
        response_text = "All songs unlocked"
    elif ability == 'skip_round':
        game.round_completed = True
        game.current_song = len(game.song_order) - 1
        game.bonus_points -= ability_data['cost']
        response_text = f"Round skipped, the game was {game.current_game}"
    elif ability == 'extra_life':
        if game.lives >= MAX_LIVES:
            return json_error('Lives are already full', 400)
        game.lives += 1
        game.bonus_points -= ability_data['cost']
        response_text = "Extra life granted"
    else:
        return json_error('Invalid ability', 400)

    game.ability_cooldowns[ability] = ability_data['cooldown']
    save_active_games()  # Save the game state after using an ability
    return json_state(game, response_text)


def handle_skip(game):
    logging.info(f"Game {game.id} used skip")
    if not game.is_infinite:
        game.lives -= 1
        if game.lives <= 0:
            game.lives = 0
            game.game_ended = True
            save_active_games()
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
    save_active_games()  # Save the game state after skipping a song
    return json_state(game, response_text)


def advance_song(game):
    if game.current_song >= len(game.song_order) - 1: #no songs left, round is over
        game.round_completed = True
    else:
        game.current_song += 1


def stage_next_round(game_id, past_games, entry):
    #runs off-request: picks the round after the current one and cuts its clips
    try:
        chosen = get_random_game(past_games)
        if chosen is None:
            entry['round'] = LIBRARY_EXHAUSTED
        elif chosen[1]:
            entry['clip_times'] = prepare_audio_for_round(
                game_id, chosen[0], chosen[1], subdir=STAGING_SUBDIR)
            entry['round'] = chosen
    except Exception: #a failed cut just means go_to_next_round does it itself
        app.logger.exception('Could not stage the next round for %s', game_id)
        entry['round'] = None
    finally:
        entry['done'].set()


def start_staging(game):
    if game.game_ended:
        return
    with staging_lock:
        if game.id in staged_rounds:
            return
        entry = {'done': threading.Event(), 'round': None}
        staged_rounds[game.id] = entry
    #the game the player is on is not in previous_games yet, so exclude it here
    past_games = game.previous_games + [game.current_game]
    threading.Thread(
        target=stage_next_round,
        args=(game.id, past_games, entry),
        daemon=True,
    ).start()


def take_staged_round(game):
    with staging_lock:
        entry = staged_rounds.pop(game.id, None)
    if entry is None:
        return None
    #worst case this waits exactly as long as cutting inline would have
    entry['done'].wait()
    return entry['clip_times'], entry['round']


def promote_staged_clips(game_id, song_count):
    staging = os.path.join(TEMP_DIR, game_id, STAGING_SUBDIR)
    for i in range(song_count):
        os.replace(
            os.path.join(staging, f"{i}.mp3"),
            os.path.join(TEMP_DIR, game_id, f"{i}.mp3"),
        )
    shutil.rmtree(staging, ignore_errors=True)


def go_to_next_round(game):
    game.round_number += 1
    game.previous_rounds.append(game.round)
    
    for ability in game.ability_cooldowns:
        if game.ability_cooldowns[ability] > 0:
            game.ability_cooldowns[ability] -= 1
    game.previous_games.append(game.current_game)
    game.shield_left = 0
    game.all_unlocked = False

    clip_times, staged = take_staged_round(game)
    if staged is LIBRARY_EXHAUSTED:
        return None #player played all games
    if staged is None: #nothing usable was prepared, fall back to cutting inline
        staged = get_random_game(game.previous_games)
        if staged is None:
            return None #player played all games
        game.clip_times = prepare_audio_for_round(game.id, staged[0], staged[1])
    else:
        game.clip_times = clip_times
        promote_staged_clips(game.id, len(staged[1]))

    game.current_song = 0
    game.current_game = staged[0]
    game.round = {"round_number":game.round_number,"guessed_correctly":False,"guessed_on":-1,"game":staged[0],"used_powerups":[]}
    logging.info(type(game.round))
    game.song_order = staged[1]
    game.round_completed = False
    logging.info(f"Game {game.id} advanced to round {game.round_number}, next game is {game.current_game}")
    start_staging(game)
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
    clip_times = prepare_audio_for_round(id, random_game[0], random_game[1])
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
        game_ended=False,
        last_interaction_date=int(datetime.datetime.now().timestamp()),
        correct_franchise=None,
        ability_cooldowns={},
        clip_times=clip_times,
        round = {"round_number":1,"guessed_correctly":False,"guessed_on":-1,"game":random_game[0],"used_powerups":[]},
        previous_rounds=[] 
    )
    logging.info(type(new_game.round))
    active_games.append(new_game)
    start_staging(new_game)
    logging.info(f"Started new game {new_game.id}, infinite = {new_game.is_infinite}, first game is {new_game.current_game}")
    return new_game


def prepare_audio_for_round(game_id, game_name, song_order, subdir=''): #puts three 20-second song clips into temp/game_id/0.mp3, temp/game_id/1.mp3, temp/game_id/2.mp3
    clip_times = []
    src_folder = os.path.join(ASSETS_DIR, game_name)
    dest_folder = os.path.join(TEMP_DIR, game_id, subdir)
    os.makedirs(dest_folder, exist_ok=True)
    for i, song in enumerate(song_order):
        src_path = os.path.join(src_folder, song)
        dest_path = os.path.join(dest_folder, f"{i}.mp3")
        audio = pydub.AudioSegment.from_file(src_path)
        max_start = len(audio) - CLIP_LENGTH_MS
        start = randint(0, max_start) if max_start > 0 else 0 #songs shorter than the clip start at 0
        clip_times.append(start)
        clip = audio[start:start + CLIP_LENGTH_MS]
        clip.export(dest_path, format="mp3")
    return clip_times

def cleanup_temp_files():
    #drop the oldest finished games so temp/ does not grow without bound
    while len(active_games) >= MAX_ACTIVE_GAMES:
        stale = next((g for g in active_games if g.game_ended), active_games[0])
        active_games.remove(stale)
        with staging_lock:
            staged_rounds.pop(stale.id, None)
        #a staging thread may still be writing in there, hence ignore_errors
        shutil.rmtree(os.path.join(TEMP_DIR, stale.id), ignore_errors=True)

def save_active_games():
    #save active games to a file for persistence
    with open(os.path.join(BASE_DIR, 'active_games.json'), 'w') as f:
        json.dump([dataclasses.asdict(g) for g in active_games], f)

def load_active_games():
    #load active games from a file
    try:
        with open(os.path.join(BASE_DIR, 'active_games.json'), 'r') as f:
            games_data = json.load(f)
            for game_data in games_data:
                game = Game(**game_data)
                if game.game_ended == True or datetime.datetime.fromtimestamp(game.last_interaction_date) < datetime.datetime.now() - datetime.timedelta(days=3):  # Skip games that started more than 3 days ago
                    continue  # Skip loading ended games
                active_games.append(game)
                start_staging(game)
            if len(active_games) == 0:
                print("No active games loaded. Starting fresh.")
    except Exception:
        print("No saved active games found. Starting fresh.")

if __name__ == '__main__':
    if not list_available_games():
        print(f"WARNING: no games found in {ASSETS_DIR} - /play will return 503 until you add some.")
    load_active_games()
    app.run('0.0.0.0', port=2137, debug=True)
