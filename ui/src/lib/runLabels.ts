// Человекочитаемый ярлык режима прогона по (task_type, content_source, content_mode).

interface RunModeFields {
  task_type?: string | null
  content_source?: string | null
  content_mode?: string | null
  run_mode?: string | null
}

export function runModeLabel(run: RunModeFields): string {
  const tt = run.task_type ?? 'post'
  if (tt === 'sitewide_link') return 'Сквозная ссылка'
  if (tt === 'homepage_link') return 'Ссылка с главной'

  const cs = run.content_source ?? 'upload_txt'
  if (cs === 'csv_direct') return 'Пост · CSV тексты'
  if (cs === 'spin_fanout') return 'Spin-fanout'
  if (cs === 'csv_campaign') {
    const m =
      run.content_mode === 'gen_per_post' ? 'генерация на пост'
      : run.content_mode === 'gen_per_row' ? 'генерация на строку'
      : run.content_mode === 'reuse' ? 'reuse из библиотеки'
      : 'кампания'
    const rm = run.run_mode === 'manual' ? ' · manual' : ''
    return `Кампания · ${m}${rm}`
  }
  return 'Пост · архив .txt'
}
