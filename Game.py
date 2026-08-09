from dataclasses import dataclass

@dataclass
class Game:
    id: str
    round_number: int
    previous_games: list
    is_infinite: bool
    lives: int #non-infinite only
    bonus_points: int #non-infinite only
    points: int #correct guesses so far
    current_game: str
    game_ended: bool
    ability_cooldowns: dict

    #bonus points - should reset after round end or correct guess
    shield_left: int
    all_unlocked: bool

    #round specific - should reset after each round
    current_song: int
    song_order: list
    round_completed: bool
    correct_franchise: bool

    #for cleanup purposes
    last_interaction_date: int
