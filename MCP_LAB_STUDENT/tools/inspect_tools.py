"""Этап 1: настоящий MCP initialize/tools/list и проверка учебной политики без запуска браузера."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from agent import McpClient, PolicyGate
from scenarios import Scenario, SCENARIOS
from student_policy import POLICIES, validate_policy


def scenario_for(variant: str) -> Scenario:
    """Сценарий по имени варианта; 'web' — безопасный read_only дефолт,
    именованный — из SCENARIOS, неизвестный — явная ошибка."""
    if variant == 'web':
        return Scenario(name='web', url='https://example.com',
                        domains=['example.com'], example_task='',
                        read_only=True)
    if variant in SCENARIOS:
        return SCENARIOS[variant]
    raise ValueError(f'неизвестный вариант {variant!r}: создайте сценарий '
                     f'в backend/scenarios.py (SCENARIOS) или используйте '
                     f'--variant web')


def main():
    """Разбор аргументов, подключение к MCP-серверу (stdio, без
    браузера), печать вердиктов политики по каталогу инструментов."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--variant', default='web',
                        help='Имя сценария из SCENARIOS (default: web)')
    parser.add_argument('--json', action='store_true',
                        help='Показать полный каталог JSON Schema')
    args = parser.parse_args()
    scenario = scenario_for(args.variant)
    rules = POLICIES.get(scenario.name)
    client = McpClient()
    try:
        tools = client.list_tools()
        gate = PolicyGate(scenario, tools=tools)
        if rules is not None:
            validate_policy(rules, {t['name'] for t in tools})
        else:
            if scenario.read_only:
                print(f'POLICIES не задан: действует read_only режим '
                      f'{scenario.name!r} — чтение/навигация разрешены, '
                      f'click/type/press запрещены.\n')
            else:
                print(f'WARNING: у сценария {scenario.name!r} нет записи '
                      f'в POLICIES — гейт работает по полям Scenario '
                      f'(read_only=False): действия открыты, это НЕ '
                      f'fail-closed. Добавьте блок в POLICIES.\n')
        print(f'MCP подключён. Сценарий: {scenario.name}. Вердикты ниже — '
              f'базовые по инструменту: имя цели, ref и доменный scope '
              f'оцениваются на реальном вызове (check_call).\n')
        for tool in tools:
            verdict = gate.verdict_for(tool['name'])
            readonly = tool.get('annotations', {}).get('readOnlyHint', False)
            print(f'{tool["name"]:25} {verdict:8} readOnlyHint={readonly}')
            print(f'  {tool["description"]}')
        if args.json:
            print(json.dumps(tools, ensure_ascii=False, indent=2))
        print('\nЗадание: создайте свой сценарий в backend/scenarios.py '
              'и правила в backend/student_policy.py; повторите проверку.')
        return 0
    finally:
        client.close()


if __name__ == '__main__':
    raise SystemExit(main())
