"""要求で使うシナリオ参照と、保存済み状態の公開世代。"""

from collections import namedtuple


ScenarioSelection = namedtuple('ScenarioSelection', 'scenario uri generation')


class ScenarioStateError(ValueError):
    """公開世代を確認できず、状態をそのまま実行できない。"""


class ScenarioLoadError(RuntimeError):
    """台本の実行を始める前に、対象版を取得できなかった。"""


def read_scenario_generation(generations, bot_name):
    if not isinstance(generations, dict):
        raise ScenarioStateError('公開世代のないセーブです。リセットしてください')
    generation = generations.get(bot_name, 0)
    if type(generation) is not int or generation < 0:
        raise ScenarioStateError('このBotの公開世代を確認できません。リセットしてください')
    return generation


def stamp_scenario_generation(status, bot_name, generation):
    generations = status.scenario_generations
    if not isinstance(generations, dict):
        # 固定版で旧保存を利用しても、他Botの不明な世代を新規扱いにしない。
        if generation is None:
            return
        raise ScenarioStateError('公開世代のないセーブです。リセットしてください')
    updated = {**generations, bot_name: generation}
    if updated != generations:
        status.scenario_generations = updated
