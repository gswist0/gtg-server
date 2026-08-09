from dataclasses import dataclass

@dataclass
class Response:
    game_id: str = ""
    response_text: str = ""
    is_correct: bool = None
    current_bonus_points: int = None
    current_points: int = None
    current_round: int = None
    current_song: int = None #highest unlocked song index
    total_songs: int = None
    lives_left: int = None
    max_lives: int = None
    shield_left: int = None
    all_unlocked: bool = None
    round_completed: bool = None
    is_infinite: bool = None
    game_ended: bool = None
    correct_answer: str = None #only revealed once the round is over
    correct_franchise: bool = None 
    ability_cooldowns: dict = None #ability name -> cooldown turns left
    clip_times: list = None #list of start times for each song clip in the current round