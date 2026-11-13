import { existsSync, mkdirSync, statSync, writeFileSync } from 'fs';
import { join, dirname } from 'path';
import { fileURLToPath } from 'url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

const fontsDir = join(__dirname, '../static/fonts');

// Create fonts directory if it doesn't exist
if (!existsSync(fontsDir)) {
  mkdirSync(fontsDir, { recursive: true });
}

const fontWeights = {
  300: {
    name: 'Light',
    url: 'https://fonts.gstatic.com/s/poppins/v21/pxiByp8kv8JHgFVrLDz8Z1xlFQ.woff2',
  },
  400: {
    name: 'Regular',
    url: 'https://fonts.gstatic.com/s/poppins/v21/pxiEyp8kv8JHgFVrJJfecg.woff2',
  },
  500: {
    name: 'Medium',
    url: 'https://fonts.gstatic.com/s/poppins/v21/pxiByp8kv8JHgFVrLGT9Z1xlFQ.woff2',
  },
  600: {
    name: 'SemiBold',
    url: 'https://fonts.gstatic.com/s/poppins/v21/pxiByp8kv8JHgFVrLEj6Z1xlFQ.woff2',
  },
  700: {
    name: 'Bold',
    url: 'https://fonts.gstatic.com/s/poppins/v21/pxiByp8kv8JHgFVrLCz7Z1xlFQ.woff2',
  },
};

// A font already on disk with content is kept (same idea as build-ffmpeg.js isValid), so repeat
// builds and offline builds need no network.
function isValid(path) {
  return existsSync(path) && statSync(path).size > 0;
}

// Download a file from URL using native fetch (Node.js 18+)
async function downloadFile(url) {
  try {
    const response = await fetch(url);
    if (!response.ok) {
      throw new Error(`Failed to download ${url}: ${response.status} ${response.statusText}`);
    }
    const arrayBuffer = await response.arrayBuffer();
    return Buffer.from(arrayBuffer);
  } catch (error) {
    console.error(`Error downloading ${url}:`, error.message);
    throw error;
  }
}

// Download all font files
async function downloadFonts() {
  try {
    // Create a CSS file with @font-face rules
    let cssContent = '/* Google Fonts - Poppins */\n';

    // Download each font file directly from Google Fonts
    for (const [weight, fontInfo] of Object.entries(fontWeights)) {
      const fontFileName = `Poppins-${fontInfo.name}.woff2`;
      const localPath = join(fontsDir, fontFileName);

      try {
        if (isValid(localPath)) {
          console.log(`Skipping ${fontFileName} (already present)`);
        } else {
          console.log(`Downloading ${fontFileName}...`);
          writeFileSync(localPath, await downloadFile(fontInfo.url));
        }

        // Add @font-face rule
        cssContent += `\n@font-face {\n`;
        cssContent += `  font-family: 'Poppins';\n`;
        cssContent += `  font-style: normal;\n`;
        cssContent += `  font-weight: ${weight};\n`;
        cssContent += `  font-display: swap;\n`;
        cssContent += `  src: local('Poppins ${fontInfo.name}'), local('Poppins-${fontInfo.name}'), `;
        cssContent += `url('/fonts/${fontFileName}') format('woff2');\n`;
        cssContent += `  unicode-range: U+0000-00FF, U+0131, U+0152-0153, U+02BB-02BC, U+02C6, U+02DA, U+02DC, U+2000-206F, U+2074, U+20AC, U+2122, U+2191, U+2193, U+2212, U+2215, U+FEFF, U+FFFD;\n`;
        cssContent += `}\n`;

        console.log(`Downloaded ${fontFileName}`);
      } catch (error) {
        console.error(`Error downloading ${fontFileName}:`, error.message);
        // Continue with other fonts even if one fails
      }
    }

    // Write the CSS file
    writeFileSync(join(fontsDir, 'poppins.css'), cssContent);
    console.log('Generated poppins.css with @font-face rules');
    console.log('All fonts downloaded successfully!');
  } catch (error) {
    console.error('Error in font download process:', error);
    process.exit(1);
  }
}

// Main function
async function main() {
  await downloadFonts();
}

main();
