/** Обёртка ECharts с glass-темой и адаптивным размером. */
import ReactECharts from 'echarts-for-react';
import type { EChartsOption } from 'echarts';

interface Props {
  option: EChartsOption;
  height?: number | string;
}

export const glassTooltip = {
  backgroundColor: 'rgba(16, 20, 44, 0.86)',
  borderColor: 'rgba(255,255,255,0.16)',
  borderWidth: 1,
  textStyle: { color: '#eef1ff', fontSize: 12 },
  extraCssText: 'backdrop-filter: blur(16px); border-radius: 14px; box-shadow: 0 10px 32px rgba(0,0,0,0.45);',
};

export const axisStyle = {
  axisLine: { lineStyle: { color: 'rgba(255,255,255,0.16)' } },
  axisLabel: { color: 'rgba(238,241,255,0.55)', fontSize: 11 },
  splitLine: { lineStyle: { color: 'rgba(255,255,255,0.06)' } },
};

export function Chart({ option, height = 260 }: Props) {
  return (
    <ReactECharts
      option={option}
      style={{ height, width: '100%' }}
      notMerge
      opts={{ renderer: 'canvas' }}
      theme={'dark'}
    />
  );
}
