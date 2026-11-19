async function drawPriceChart(section) {
  const caption = section.querySelector('.chart-caption');
  const canvas = section.querySelector('canvas');
  const details = section.querySelector('.chart-values');
  try {
    const response = await fetch(section.dataset.chartUrl);
    if (!response.ok) throw new Error('가격 그래프를 조회할 수 없어요.');
    const chartData = await response.json();
    const money = new Intl.NumberFormat('ko-KR', { style: 'currency', currency: chartData.currency });
    caption.textContent = `${chartData.source} · ${chartData.currency}`;
    const list = details.querySelector('ul');
    chartData.labels.forEach((label, index) => {
      const item = document.createElement('li');
      item.textContent = `${label} · ${money.format(chartData.values[index])}`;
      list.append(item);
    });
    details.hidden = false;
    if (typeof Chart === 'undefined') {
      caption.textContent += ' · Chart.js 연결 오류. 아래에서 가격을 확인할 수 있어요.';
      details.open = true;
      return;
    }
    canvas.hidden = false;
    new Chart(canvas, {
      type: 'line',
      data: {
        labels: chartData.labels,
        datasets: [{ data: chartData.values, borderColor: '#244da3', borderWidth: 2, pointRadius: 2, tension: 0 }],
      },
      options: {
        plugins: {
          legend: { display: false },
          tooltip: { callbacks: { label: context => money.format(context.parsed.y) } },
        },
        scales: { y: { ticks: { callback: value => money.format(value) } } },
      },
    });
  } catch {
    canvas.hidden = true;
    caption.textContent = '가격 그래프를 불러오지 못했어요. 잠시 후 다시 시도해 주세요.';
  }
}

const chartSection = document.querySelector('[data-chart-url]');
if (chartSection) drawPriceChart(chartSection);
