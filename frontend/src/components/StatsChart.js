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
        // Response from backend has:
        //   { personal: [{ date:"YYYY-MM-DD", tokens: ...}, ...],
        //     global:   [{ date:"YYYY-MM-DD", tokens: ...}, ...],
        //     personal_total: <number>,
        //     global_total:   <number> }
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
        // Dates are in ISO format so alphabetic sort works (earlier dates will come first)
        combinedDates.sort();
        
        // For each combined date, pull tokens from the personal and global lookup maps (or default to 0)
        const personalTokens = combinedDates.map(date => personalMap[date] || 0);
        const globalTokens = combinedDates.map(date => globalMap[date] || 0);

        setChartData({
          labels: combinedDates,
          datasets: [
            {
              label: 'Personal Daily Tokens',
              data: personalTokens,
              borderColor: 'rgba(97, 218, 251, 1)',
              backgroundColor: 'rgba(97, 218, 251, 0.2)',
              pointRadius: 5,
              pointBackgroundColor: (context) => {
                const index = context.dataIndex;
                return (index === combinedDates.length - 1) ? '#ffcc00' : 'rgba(97, 218, 251, 1)';
              },
            },
            {
              label: 'Global Daily Tokens',
              data: globalTokens,
              borderColor: 'rgba(162, 155, 254, 1)',
              backgroundColor: 'rgba(162, 155, 254, 0.2)',
              pointRadius: 5,
              pointBackgroundColor: (context) => {
                const index = context.dataIndex;
                return (index === combinedDates.length - 1) ? '#ffcc00' : 'rgba(162, 155, 254, 1)';
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
    return <div style={{ color: "#e0e0e0" }}>Loading Chart...</div>;
  }

  // Chart options, including color settings for dark mode.
  const options = {
    responsive: true,
    plugins: {
      title: {
        display: true,
        text: 'Daily Token Progress',
        color: "#e0e0e0",
      },
      legend: {
        labels: {
          color: "#e0e0e0",
        },
      },
    },
    scales: {
      x: {
        ticks: { color: "#e0e0e0" },
        grid: { color: "#333" },
      },
      y: {
        ticks: { color: "#e0e0e0" },
        grid: { color: "#333" },
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
            borderRadius: '4px',
            color: '#e0e0e0',
            fontSize: '0.9em'
          }}
        >
          Total: {totals.personal_total} / {totals.global_total}
      </div>
    </div>
  );
};

export default StatsChart;