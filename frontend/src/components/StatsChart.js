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

const StatsChart = () => {
  const [chartData, setChartData] = useState(null);

  useEffect(() => {
    // Fetch statistics from the backend
    api.get('/stats')
      .then(response => {
        // Example response structure:
        // response.data.personal: [ { date: "2023-10-09", tokens: 150 }, ... ]
        // response.data.global:   [ { date: "2023-10-09", tokens: 4567 }, ... ]
        const personal = response.data.personal;
        const global = response.data.global;
        
        // Assuming both arrays use the same dates, otherwise build a union of dates
        const dates = personal.map(item => item.date);
        const personalTokens = personal.map(item => item.tokens);
        const globalTokens = global.map(item => item.tokens);
        
        setChartData({
          labels: dates,
          datasets: [
            {
              label: 'Personal Daily Tokens',
              data: personalTokens,
              borderColor: 'rgba(97, 218, 251, 1)', // Blue color
              backgroundColor: 'rgba(97, 218, 251, 0.2)',
              pointRadius: 5,
              // Highlight today's point (assumed to be at the end) with a different color
              pointBackgroundColor: (context) => {
                const index = context.dataIndex;
                return (index === dates.length - 1) ? '#ffcc00' : 'rgba(97, 218, 251, 1)';
              },
            },
            {
              label: 'Global Daily Tokens',
              data: globalTokens,
              borderColor: 'rgba(162, 155, 254, 1)', // Purple color
              backgroundColor: 'rgba(162, 155, 254, 0.2)',
              pointRadius: 5,
              pointBackgroundColor: (context) => {
                const index = context.dataIndex;
                return (index === dates.length - 1) ? '#ffcc00' : 'rgba(162, 155, 254, 1)';
              },
            },
          ],
        });
      })
      .catch(error => {
        console.error("Error fetching stats:", error);
      });
  }, []);

  if (!chartData) {
    return <div style={{ color: "#e0e0e0" }}>Loading Chart...</div>;
  }

  // Chart options including dark-mode adjustments for labels and grid lines.
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
    <div>
      <Line data={chartData} options={options} />
    </div>
  );
};

export default StatsChart;