import React, { useEffect, useState } from 'react';
import { Line } from 'react-chartjs-2';
import {
  Chart as ChartJS,
  CategoryScale,
  LinearScale,
  PointElement,
  LineElement,
  Title,
  Tooltip,
  Legend,
} from 'chart.js';
import api from '../api';

// Register the necessary Chart.js components
ChartJS.register(
  CategoryScale,
  LinearScale,
  PointElement,
  LineElement,
  Title,
  Tooltip,
  Legend
);

const StatsChart = ({ username }) => {
  const [chartData, setChartData] = useState(null);
  const [totals, setTotals] = useState({ personal_total: 0, global_total: 0 });

  useEffect(() => {
    // Use the /stats endpoint (with username if provided)
    const endpoint = username ? `/stats/${username}` : '/stats';
    api.get(endpoint)
      .then(response => {
        const personal = response.data.personal;
        const global = response.data.global;
        const personal_total = response.data.personal_total;
        const global_total = response.data.global_total;

        // Build lookup maps for quick access
        const personalMap = {};
        personal.forEach(item => {
          personalMap[item.date] = item.tokens;
        });
        const globalMap = {};
        global.forEach(item => {
          globalMap[item.date] = item.tokens;
        });

        // Build the union of dates as the x-axis.
        const dateSet = new Set([...personal.map(p => p.date), ...global.map(g => g.date)]);
        let combinedDates = Array.from(dateSet);
        combinedDates.sort();

        const personalTokens = combinedDates.map(date => personalMap[date] || 0);
        const globalTokens = combinedDates.map(date => globalMap[date] || 0);

        setChartData({
          labels: combinedDates,
          datasets: [
            {
              label: 'Personal Daily Tokens',
              data: personalTokens,
              borderColor: '#c4956a',
              backgroundColor: 'rgba(196, 149, 106, 0.2)',
              pointRadius: 5,
              pointBackgroundColor: (context) => {
                const index = context.dataIndex;
                return (index === combinedDates.length - 1) ? '#e8e2d6' : '#c4956a';
              },
            },
            {
              label: 'Global Daily Tokens',
              data: globalTokens,
              borderColor: '#a89a8a',
              backgroundColor: 'rgba(168, 154, 138, 0.2)',
              pointRadius: 5,
              pointBackgroundColor: (context) => {
                const index = context.dataIndex;
                return (index === combinedDates.length - 1) ? '#e8e2d6' : '#a89a8a';
              },
            },
          ],
        });
        setTotals({ personal_total, global_total });
      })
      .catch(error => {
        console.error("Error fetching stats:", error);
      });
  }, [username]);

  if (!chartData) {
    return <div style={{ color: "var(--text-muted)", fontFamily: "var(--sans)" }}>Loading Chart...</div>;
  }

  // Chart options, including color settings for dark mode.
  const options = {
    responsive: true,
    plugins: {
      title: {
        display: true,
        text: 'Daily Token Progress',
        color: "var(--text-primary)",
        font: {
          family: "'Outfit', sans-serif",
          weight: 300,
        },
      },
      legend: {
        labels: {
          color: "#a89a8a",
          font: {
            family: "'Outfit', sans-serif",
            weight: 300,
          },
        },
      },
    },
    scales: {
      x: {
        ticks: {
          color: "#6d635a",
          font: { family: "'Outfit', sans-serif", weight: 300 },
        },
        grid: { color: "#2a2725" },
      },
      y: {
        ticks: {
          color: "#6d635a",
          font: { family: "'Outfit', sans-serif", weight: 300 },
        },
        grid: { color: "#2a2725" },
      },
    },
  };

  return (
    <div style={{ position: 'relative', width: '600px', height: '400px' }}>
      <Line data={chartData} options={options} />
      <div
          style={{
            position: 'absolute',
            bottom: '300px',
            right: '250px',
            backgroundColor: 'rgba(0, 0, 0, 0.55)',
            borderRadius: '6px',
            color: 'var(--text-secondary)',
            fontSize: '0.85em',
            fontFamily: 'var(--sans)',
            fontWeight: 300,
            padding: '2px 8px',
          }}
        >
          Total: {totals.personal_total} / {totals.global_total}
      </div>
    </div>
  );
};

export default StatsChart;
