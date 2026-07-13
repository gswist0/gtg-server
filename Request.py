from dataclasses import dataclass

@dataclass
class Request:
    game_id: str = ""
    action: str ="" #guess, next, ability, skip, start
    guessed_game: str = ""
    ability_used: str = "" #shield, unlock, skip_round, extra_life
    infinite: bool = False#for starting a new game
    