from dataclasses import dataclass

@dataclass
class Response:
    game_id: str = ""
    response_text: str = ""
    is_correct: bool = None
    current_bonus_points: int = None
    current_round: int = None
    current_song: int = None
    lives_left: int = None
    shield_left: int = None
    is_infinite: bool = None
    game_ended: bool = None