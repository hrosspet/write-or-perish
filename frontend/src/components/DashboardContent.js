import React from 'react';
import StatsChart from './StatsChart';

function DashboardContent({ dashboardData }) {
  return (
    <div style={{ marginBottom: '30px' }}>
      {/* You can optionally rename the header if needed */}
      
      {/* Chart container is set as relative. The overlay will be absolutely positioned within it. */}
      <div
        style={{
          position: 'relative',
          width: '600px',
          height: '400px',
          display: 'inline-block'
        }}
      >
        <StatsChart />
        {/* Overlay with total stats */}
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
          Total: {dashboardData.stats.total_tokens} / {dashboardData.stats.global_tokens}
        </div>
      </div>
    </div>
  );
}

export default DashboardContent;