"""Language selection shared by direct Python setup and the configuration wizard."""

from setup_ui import TerminalUI


def choose_language():
    # Ask before discovery or configuration so the first decision controls all prompts.
    ui = TerminalUI()
    choices = {'1': 'en', 'en': 'en', 'english': 'en',
               '2': 'zh-CN', 'zh': 'zh-CN', 'zh-cn': 'zh-CN', '中文': 'zh-CN'}
    while True:
        answer = ui.select(
            'Select language / 请选择语言:',
            ['English', '中文'],
            instruction='Use ↑/↓ and Enter / 使用 ↑/↓ 后按 Enter',
        )
        if answer is None:
            raise EOFError()
        answer = str(answer).strip().lower()
        if answer in ('q', 'quit'):
            raise EOFError()
        if answer in choices:
            return choices[answer]
        ui.message('Invalid choice; enter 1 or 2 / 选择无效，请输入 1 或 2。')
