# sleep

一个原子动作：躺下睡觉，恢复体力、消退疲惫。

这是恢复类动作的核心。**终态语义不属于它**——同一个 sleep，在 rest 里是收尾点（睡饱了 rest 就完成），在别的 activity 里可能只是中途小憩。睡没睡够、要不要继续，由所在 activity 的 terminal_when 判断。
