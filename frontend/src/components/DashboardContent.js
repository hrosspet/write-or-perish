import React from 'react';
import StatsChart from './StatsChart';

function DashboardContent({ dashboardData }) {
  return (
    <div style={{ marginBottom: '30px' }}>
      <h3 style={{ color: '#e0e0e0' }}>Daily Progress</h3>
      {/* Wrap the chart in a container to control its size */}
      <div style={{ width: '600px', height: '400px', align: "left" }}>
        <StatsChart />
      </div>
      <div style={{ marginTop: '20px', color: '#e0e0e0' }}>
        <p>
          <strong>Today's Personal Tokens:</strong>{' '}
          {dashboardData.stats.daily_tokens}
        </p>
        <p>
          <strong>Today's Global Tokens:</strong>{' '}
          {dashboardData.stats.daily_global_tokens || 'N/A'}
        </p>
        <p>
          <strong>Personal Total:</strong> {dashboardData.stats.total_tokens}
        </p>
        <p>
          <strong>Global Total:</strong> {dashboardData.stats.global_tokens}
        </p>
      </div>
    </div>
  );
}

export default DashboardContent;