import React from 'react';
import StatsChart from './StatsChart';

function DashboardContent({ dashboardData, username }) {
  return (
    <div style={{ marginBottom: '30px' }}>
      <div
        style={{
          position: 'relative',
          width: '600px',
          height: '400px',
          display: 'inline-block'
        }}
      >
        <StatsChart username={username} />
      </div>
    </div>
  );
}

export default DashboardContent;