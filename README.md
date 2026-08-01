# gtg-server

Flask backend for *Guess the Game by Song*. It owns the whole game: it picks the
round's game, cuts the 20-second clips, tracks lives / bonus points / shields and
decides whether a guess is right. The React frontend only renders what this
server returns.

## Setup

```bash
pip install flask pydub
```

`pydub` shells out to **ffmpeg**, so ffmpeg must be on `PATH`.

### Audio assets

The server plays whatever it finds in `assets/games`:

```
gtg-server/
  assets/games/
    Dark Souls/
      0.mp3
      1.mp3
      2.mp3
    Elden Ring/
      ...
```

The **folder name is the answer** the player has to type, and it is also what
`/autofill` feeds to the guess autocomplete. Three or more songs per game is
ideal (the server picks 3 at random each round). Drop the files in yourself -
`assets/games/` is gitignored, so every machine brings its own library.

File names inside a game folder do not matter; anything that is not an audio
file (`.mp3`, `.wav`, `.ogg`, `.flac`, `.m4a`) is skipped.

Without any assets, `/play` answers `503` and `/autofill` returns an empty list.

## Running

```bash
python main.py            # http://0.0.0.0:2137
```

Paths are resolved relative to this file, so the working directory does not
matter. Two environment variables are read:

| Variable               | Default            | Purpose                              |
| ---------------------- | ------------------ | ------------------------------------ |
| `FLASK_SECRET_KEY`     | random per boot    | Flask session key                     |
| `CORS_ALLOWED_ORIGIN`  | `*`                | Origin allowed to call the API        |

Game state lives in memory, so restarting the server drops every game in
progress (the frontend falls back to the mode picker).

## API

| Method | Endpoint                          | Purpose                                       |
| ------ | --------------------------------- | --------------------------------------------- |
| `GET`  | `/autofill`                       | `{ "games": [...] }` - autocomplete source     |
| `POST` | `/play`                           | Every game action, see below                   |
| `GET`  | `/game_state/<game_id>`           | Current state, used to resume after a reload   |
| `GET`  | `/get_audio/<game_id>/<song_no>`  | The 20 s clip; `403` while the song is locked  |

`POST /play` body:

```jsonc
{
  "action": "start",       // start | guess | next | ability | skip
  "game_id": "uuid",       // required for everything except "start"
  "guessed_game": "Hades", // action = guess
  "ability_used": "shield",// action = ability: shield | unlock | skip_round | extra_life
  "infinite": false        // action = start: endless mode
}
```

Every successful call returns the same state object (`Response.py`):
`game_id`, `response_text`, `is_correct`, `current_points`,
`current_bonus_points`, `current_round`, `current_song`, `total_songs`,
`lives_left`, `max_lives`, `shield_left`, `all_unlocked`, `round_completed`,
`is_infinite`, `game_ended` and `correct_answer` (only once the round is over).
Errors are `{"error": "..."}` with a 4xx/5xx code.

## Game rules

* Each round has 3 clips; a wrong guess or a skip unlocks the next one.
* **Normal mode** - 5 lives, and a correct guess on the first clip earns a bonus
  point that pays for one ability.
* **Endless mode** - no lives are lost and abilities are disabled; the run ends
  only when the library runs out of games.
