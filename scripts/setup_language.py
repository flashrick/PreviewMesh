"""Language selection shared by direct Python setup and the configuration wizard."""


def choose_language():
    # Ask before discovery or configuration so the first decision controls all prompts.
    print('Select language / 请选择语言:\n  1. English\n  2. 中文', flush=True)
    choices = {'1': 'en', 'en': 'en', 'english': 'en',
               '2': 'zh-CN', 'zh': 'zh-CN', 'zh-cn': 'zh-CN', '中文': 'zh-CN'}
    while True:
        answer = input('Choice / 请选择 (1/2, q to exit / 退出): ').strip().lower()
        if answer == 'q':
            raise EOFError()
        if answer in choices:
            return choices[answer]
        print('Invalid choice; enter 1 or 2 / 选择无效，请输入 1 或 2。')
