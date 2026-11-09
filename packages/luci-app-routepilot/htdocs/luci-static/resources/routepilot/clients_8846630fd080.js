'use strict';
'require baseclass';
'require fs';
'require ui';
'require poll';
// Offline QR encoder, qrcode-generator 2.0.4 (MIT). UMD wrapper removed.
//---------------------------------------------------------------------
//
// QR Code Generator for JavaScript
//
// Copyright (c) 2009 Kazuhiko Arase
//
// URL: http://www.d-project.com/
//
// Licensed under the MIT license:
//  http://www.opensource.org/licenses/mit-license.php
//
// The word 'QR Code' is registered trademark of
// DENSO WAVE INCORPORATED
//  http://www.denso-wave.com/qrcode/faqpatent-e.html
//
//---------------------------------------------------------------------

var qrcode = function() {

  //---------------------------------------------------------------------
  // qrcode
  //---------------------------------------------------------------------

  /**
   * qrcode
   * @param typeNumber 1 to 40
   * @param errorCorrectionLevel 'L','M','Q','H'
   */
  var qrcode = function(typeNumber, errorCorrectionLevel) {

    var PAD0 = 0xEC;
    var PAD1 = 0x11;

    var _typeNumber = typeNumber;
    var _errorCorrectionLevel = QRErrorCorrectionLevel[errorCorrectionLevel];
    var _modules = null;
    var _moduleCount = 0;
    var _dataCache = null;
    var _dataList = [];

    var _this = {};

    var makeImpl = function(test, maskPattern) {

      _moduleCount = _typeNumber * 4 + 17;
      _modules = function(moduleCount) {
        var modules = new Array(moduleCount);
        for (var row = 0; row < moduleCount; row += 1) {
          modules[row] = new Array(moduleCount);
          for (var col = 0; col < moduleCount; col += 1) {
            modules[row][col] = null;
          }
        }
        return modules;
      }(_moduleCount);

      setupPositionProbePattern(0, 0);
      setupPositionProbePattern(_moduleCount - 7, 0);
      setupPositionProbePattern(0, _moduleCount - 7);
      setupPositionAdjustPattern();
      setupTimingPattern();
      setupTypeInfo(test, maskPattern);

      if (_typeNumber >= 7) {
        setupTypeNumber(test);
      }

      if (_dataCache == null) {
        _dataCache = createData(_typeNumber, _errorCorrectionLevel, _dataList);
      }

      mapData(_dataCache, maskPattern);
    };

    var setupPositionProbePattern = function(row, col) {

      for (var r = -1; r <= 7; r += 1) {

        if (row + r <= -1 || _moduleCount <= row + r) continue;

        for (var c = -1; c <= 7; c += 1) {

          if (col + c <= -1 || _moduleCount <= col + c) continue;

          if ( (0 <= r && r <= 6 && (c == 0 || c == 6) )
              || (0 <= c && c <= 6 && (r == 0 || r == 6) )
              || (2 <= r && r <= 4 && 2 <= c && c <= 4) ) {
            _modules[row + r][col + c] = true;
          } else {
            _modules[row + r][col + c] = false;
          }
        }
      }
    };

    var getBestMaskPattern = function() {

      var minLostPoint = 0;
      var pattern = 0;

      for (var i = 0; i < 8; i += 1) {

        makeImpl(true, i);

        var lostPoint = QRUtil.getLostPoint(_this);

        if (i == 0 || minLostPoint > lostPoint) {
          minLostPoint = lostPoint;
          pattern = i;
        }
      }

      return pattern;
    };

    var setupTimingPattern = function() {

      for (var r = 8; r < _moduleCount - 8; r += 1) {
        if (_modules[r][6] != null) {
          continue;
        }
        _modules[r][6] = (r % 2 == 0);
      }

      for (var c = 8; c < _moduleCount - 8; c += 1) {
        if (_modules[6][c] != null) {
          continue;
        }
        _modules[6][c] = (c % 2 == 0);
      }
    };

    var setupPositionAdjustPattern = function() {

      var pos = QRUtil.getPatternPosition(_typeNumber);

      for (var i = 0; i < pos.length; i += 1) {

        for (var j = 0; j < pos.length; j += 1) {

          var row = pos[i];
          var col = pos[j];

          if (_modules[row][col] != null) {
            continue;
          }

          for (var r = -2; r <= 2; r += 1) {

            for (var c = -2; c <= 2; c += 1) {

              if (r == -2 || r == 2 || c == -2 || c == 2
                  || (r == 0 && c == 0) ) {
                _modules[row + r][col + c] = true;
              } else {
                _modules[row + r][col + c] = false;
              }
            }
          }
        }
      }
    };

    var setupTypeNumber = function(test) {

      var bits = QRUtil.getBCHTypeNumber(_typeNumber);

      for (var i = 0; i < 18; i += 1) {
        var mod = (!test && ( (bits >> i) & 1) == 1);
        _modules[Math.floor(i / 3)][i % 3 + _moduleCount - 8 - 3] = mod;
      }

      for (var i = 0; i < 18; i += 1) {
        var mod = (!test && ( (bits >> i) & 1) == 1);
        _modules[i % 3 + _moduleCount - 8 - 3][Math.floor(i / 3)] = mod;
      }
    };

    var setupTypeInfo = function(test, maskPattern) {

      var data = (_errorCorrectionLevel << 3) | maskPattern;
      var bits = QRUtil.getBCHTypeInfo(data);

      // vertical
      for (var i = 0; i < 15; i += 1) {

        var mod = (!test && ( (bits >> i) & 1) == 1);

        if (i < 6) {
          _modules[i][8] = mod;
        } else if (i < 8) {
          _modules[i + 1][8] = mod;
        } else {
          _modules[_moduleCount - 15 + i][8] = mod;
        }
      }

      // horizontal
      for (var i = 0; i < 15; i += 1) {

        var mod = (!test && ( (bits >> i) & 1) == 1);

        if (i < 8) {
          _modules[8][_moduleCount - i - 1] = mod;
        } else if (i < 9) {
          _modules[8][15 - i - 1 + 1] = mod;
        } else {
          _modules[8][15 - i - 1] = mod;
        }
      }

      // fixed module
      _modules[_moduleCount - 8][8] = (!test);
    };

    var mapData = function(data, maskPattern) {

      var inc = -1;
      var row = _moduleCount - 1;
      var bitIndex = 7;
      var byteIndex = 0;
      var maskFunc = QRUtil.getMaskFunction(maskPattern);

      for (var col = _moduleCount - 1; col > 0; col -= 2) {

        if (col == 6) col -= 1;

        while (true) {

          for (var c = 0; c < 2; c += 1) {

            if (_modules[row][col - c] == null) {

              var dark = false;

              if (byteIndex < data.length) {
                dark = ( ( (data[byteIndex] >>> bitIndex) & 1) == 1);
              }

              var mask = maskFunc(row, col - c);

              if (mask) {
                dark = !dark;
              }

              _modules[row][col - c] = dark;
              bitIndex -= 1;

              if (bitIndex == -1) {
                byteIndex += 1;
                bitIndex = 7;
              }
            }
          }

          row += inc;

          if (row < 0 || _moduleCount <= row) {
            row -= inc;
            inc = -inc;
            break;
          }
        }
      }
    };

    var createBytes = function(buffer, rsBlocks) {

      var offset = 0;

      var maxDcCount = 0;
      var maxEcCount = 0;

      var dcdata = new Array(rsBlocks.length);
      var ecdata = new Array(rsBlocks.length);

      for (var r = 0; r < rsBlocks.length; r += 1) {

        var dcCount = rsBlocks[r].dataCount;
        var ecCount = rsBlocks[r].totalCount - dcCount;

        maxDcCount = Math.max(maxDcCount, dcCount);
        maxEcCount = Math.max(maxEcCount, ecCount);

        dcdata[r] = new Array(dcCount);

        for (var i = 0; i < dcdata[r].length; i += 1) {
          dcdata[r][i] = 0xff & buffer.getBuffer()[i + offset];
        }
        offset += dcCount;

        var rsPoly = QRUtil.getErrorCorrectPolynomial(ecCount);
        var rawPoly = qrPolynomial(dcdata[r], rsPoly.getLength() - 1);

        var modPoly = rawPoly.mod(rsPoly);
        ecdata[r] = new Array(rsPoly.getLength() - 1);
        for (var i = 0; i < ecdata[r].length; i += 1) {
          var modIndex = i + modPoly.getLength() - ecdata[r].length;
          ecdata[r][i] = (modIndex >= 0)? modPoly.getAt(modIndex) : 0;
        }
      }

      var totalCodeCount = 0;
      for (var i = 0; i < rsBlocks.length; i += 1) {
        totalCodeCount += rsBlocks[i].totalCount;
      }

      var data = new Array(totalCodeCount);
      var index = 0;

      for (var i = 0; i < maxDcCount; i += 1) {
        for (var r = 0; r < rsBlocks.length; r += 1) {
          if (i < dcdata[r].length) {
            data[index] = dcdata[r][i];
            index += 1;
          }
        }
      }

      for (var i = 0; i < maxEcCount; i += 1) {
        for (var r = 0; r < rsBlocks.length; r += 1) {
          if (i < ecdata[r].length) {
            data[index] = ecdata[r][i];
            index += 1;
          }
        }
      }

      return data;
    };

    var createData = function(typeNumber, errorCorrectionLevel, dataList) {

      var rsBlocks = QRRSBlock.getRSBlocks(typeNumber, errorCorrectionLevel);

      var buffer = qrBitBuffer();

      for (var i = 0; i < dataList.length; i += 1) {
        var data = dataList[i];
        buffer.put(data.getMode(), 4);
        buffer.put(data.getLength(), QRUtil.getLengthInBits(data.getMode(), typeNumber) );
        data.write(buffer);
      }

      // calc num max data.
      var totalDataCount = 0;
      for (var i = 0; i < rsBlocks.length; i += 1) {
        totalDataCount += rsBlocks[i].dataCount;
      }

      if (buffer.getLengthInBits() > totalDataCount * 8) {
        throw 'code length overflow. ('
          + buffer.getLengthInBits()
          + '>'
          + totalDataCount * 8
          + ')';
      }

      // end code
      if (buffer.getLengthInBits() + 4 <= totalDataCount * 8) {
        buffer.put(0, 4);
      }

      // padding
      while (buffer.getLengthInBits() % 8 != 0) {
        buffer.putBit(false);
      }

      // padding
      while (true) {

        if (buffer.getLengthInBits() >= totalDataCount * 8) {
          break;
        }
        buffer.put(PAD0, 8);

        if (buffer.getLengthInBits() >= totalDataCount * 8) {
          break;
        }
        buffer.put(PAD1, 8);
      }

      return createBytes(buffer, rsBlocks);
    };

    _this.addData = function(data, mode) {

      mode = mode || 'Byte';

      var newData = null;

      switch(mode) {
      case 'Numeric' :
        newData = qrNumber(data);
        break;
      case 'Alphanumeric' :
        newData = qrAlphaNum(data);
        break;
      case 'Byte' :
        newData = qr8BitByte(data);
        break;
      case 'Kanji' :
        newData = qrKanji(data);
        break;
      default :
        throw 'mode:' + mode;
      }

      _dataList.push(newData);
      _dataCache = null;
    };

    _this.isDark = function(row, col) {
      if (row < 0 || _moduleCount <= row || col < 0 || _moduleCount <= col) {
        throw row + ',' + col;
      }
      return _modules[row][col];
    };

    _this.getModuleCount = function() {
      return _moduleCount;
    };

    _this.make = function() {
      if (_typeNumber < 1) {
        var typeNumber = 1;

        for (; typeNumber < 40; typeNumber++) {
          var rsBlocks = QRRSBlock.getRSBlocks(typeNumber, _errorCorrectionLevel);
          var buffer = qrBitBuffer();

          for (var i = 0; i < _dataList.length; i++) {
            var data = _dataList[i];
            buffer.put(data.getMode(), 4);
            buffer.put(data.getLength(), QRUtil.getLengthInBits(data.getMode(), typeNumber) );
            data.write(buffer);
          }

          var totalDataCount = 0;
          for (var i = 0; i < rsBlocks.length; i++) {
            totalDataCount += rsBlocks[i].dataCount;
          }

          if (buffer.getLengthInBits() <= totalDataCount * 8) {
            break;
          }
        }

        _typeNumber = typeNumber;
      }

      makeImpl(false, getBestMaskPattern() );
    };

    _this.createTableTag = function(cellSize, margin) {

      cellSize = cellSize || 2;
      margin = (typeof margin == 'undefined')? cellSize * 4 : margin;

      var qrHtml = '';

      qrHtml += '<table style="';
      qrHtml += ' border-width: 0px; border-style: none;';
      qrHtml += ' border-collapse: collapse;';
      qrHtml += ' padding: 0px; margin: ' + margin + 'px;';
      qrHtml += '">';
      qrHtml += '<tbody>';

      for (var r = 0; r < _this.getModuleCount(); r += 1) {

        qrHtml += '<tr>';

        for (var c = 0; c < _this.getModuleCount(); c += 1) {
          qrHtml += '<td style="';
          qrHtml += ' border-width: 0px; border-style: none;';
          qrHtml += ' border-collapse: collapse;';
          qrHtml += ' padding: 0px; margin: 0px;';
          qrHtml += ' width: ' + cellSize + 'px;';
          qrHtml += ' height: ' + cellSize + 'px;';
          qrHtml += ' background-color: ';
          qrHtml += _this.isDark(r, c)? '#000000' : '#ffffff';
          qrHtml += ';';
          qrHtml += '"/>';
        }

        qrHtml += '</tr>';
      }

      qrHtml += '</tbody>';
      qrHtml += '</table>';

      return qrHtml;
    };

    _this.createSvgTag = function(cellSize, margin, alt, title) {

      var opts = {};
      if (typeof arguments[0] == 'object') {
        // Called by options.
        opts = arguments[0];
        // overwrite cellSize and margin.
        cellSize = opts.cellSize;
        margin = opts.margin;
        alt = opts.alt;
        title = opts.title;
      }

      cellSize = cellSize || 2;
      margin = (typeof margin == 'undefined')? cellSize * 4 : margin;

      // Compose alt property surrogate
      alt = (typeof alt === 'string') ? {text: alt} : alt || {};
      alt.text = alt.text || null;
      alt.id = (alt.text) ? alt.id || 'qrcode-description' : null;

      // Compose title property surrogate
      title = (typeof title === 'string') ? {text: title} : title || {};
      title.text = title.text || null;
      title.id = (title.text) ? title.id || 'qrcode-title' : null;

      var size = _this.getModuleCount() * cellSize + margin * 2;
      var c, mc, r, mr, qrSvg='', rect;

      rect = 'l' + cellSize + ',0 0,' + cellSize +
        ' -' + cellSize + ',0 0,-' + cellSize + 'z ';

      qrSvg += '<svg version="1.1" xmlns="http://www.w3.org/2000/svg"';
      qrSvg += !opts.scalable ? ' width="' + size + 'px" height="' + size + 'px"' : '';
      qrSvg += ' viewBox="0 0 ' + size + ' ' + size + '" ';
      qrSvg += ' preserveAspectRatio="xMinYMin meet"';
      qrSvg += (title.text || alt.text) ? ' role="img" aria-labelledby="' +
          escapeXml([title.id, alt.id].join(' ').trim() ) + '"' : '';
      qrSvg += '>';
      qrSvg += (title.text) ? '<title id="' + escapeXml(title.id) + '">' +
          escapeXml(title.text) + '</title>' : '';
      qrSvg += (alt.text) ? '<description id="' + escapeXml(alt.id) + '">' +
          escapeXml(alt.text) + '</description>' : '';
      qrSvg += '<rect width="100%" height="100%" fill="white" cx="0" cy="0"/>';
      qrSvg += '<path d="';

      for (r = 0; r < _this.getModuleCount(); r += 1) {
        mr = r * cellSize + margin;
        for (c = 0; c < _this.getModuleCount(); c += 1) {
          if (_this.isDark(r, c) ) {
            mc = c*cellSize+margin;
            qrSvg += 'M' + mc + ',' + mr + rect;
          }
        }
      }

      qrSvg += '" stroke="transparent" fill="black"/>';
      qrSvg += '</svg>';

      return qrSvg;
    };

    _this.createDataURL = function(cellSize, margin) {

      cellSize = cellSize || 2;
      margin = (typeof margin == 'undefined')? cellSize * 4 : margin;

      var size = _this.getModuleCount() * cellSize + margin * 2;
      var min = margin;
      var max = size - margin;

      return createDataURL(size, size, function(x, y) {
        if (min <= x && x < max && min <= y && y < max) {
          var c = Math.floor( (x - min) / cellSize);
          var r = Math.floor( (y - min) / cellSize);
          return _this.isDark(r, c)? 0 : 1;
        } else {
          return 1;
        }
      } );
    };

    _this.createImgTag = function(cellSize, margin, alt) {

      cellSize = cellSize || 2;
      margin = (typeof margin == 'undefined')? cellSize * 4 : margin;

      var size = _this.getModuleCount() * cellSize + margin * 2;

      var img = '';
      img += '<img';
      img += '\u0020src="';
      img += _this.createDataURL(cellSize, margin);
      img += '"';
      img += '\u0020width="';
      img += size;
      img += '"';
      img += '\u0020height="';
      img += size;
      img += '"';
      if (alt) {
        img += '\u0020alt="';
        img += escapeXml(alt);
        img += '"';
      }
      img += '/>';

      return img;
    };

    var escapeXml = function(s) {
      var escaped = '';
      for (var i = 0; i < s.length; i += 1) {
        var c = s.charAt(i);
        switch(c) {
        case '<': escaped += '&lt;'; break;
        case '>': escaped += '&gt;'; break;
        case '&': escaped += '&amp;'; break;
        case '"': escaped += '&quot;'; break;
        default : escaped += c; break;
        }
      }
      return escaped;
    };

    var _createHalfASCII = function(margin) {
      var cellSize = 1;
      margin = (typeof margin == 'undefined')? cellSize * 2 : margin;

      var size = _this.getModuleCount() * cellSize + margin * 2;
      var min = margin;
      var max = size - margin;

      var y, x, r1, r2, p;

      var blocks = {
        '██': '█',
        '█ ': '▀',
        ' █': '▄',
        '  ': ' '
      };

      var blocksLastLineNoMargin = {
        '██': '▀',
        '█ ': '▀',
        ' █': ' ',
        '  ': ' '
      };

      var ascii = '';
      for (y = 0; y < size; y += 2) {
        r1 = Math.floor((y - min) / cellSize);
        r2 = Math.floor((y + 1 - min) / cellSize);
        for (x = 0; x < size; x += 1) {
          p = '█';

          if (min <= x && x < max && min <= y && y < max && _this.isDark(r1, Math.floor((x - min) / cellSize))) {
            p = ' ';
          }

          if (min <= x && x < max && min <= y+1 && y+1 < max && _this.isDark(r2, Math.floor((x - min) / cellSize))) {
            p += ' ';
          }
          else {
            p += '█';
          }

          // Output 2 characters per pixel, to create full square. 1 character per pixels gives only half width of square.
          ascii += (margin < 1 && y+1 >= max) ? blocksLastLineNoMargin[p] : blocks[p];
        }

        ascii += '\n';
      }

      if (size % 2 && margin > 0) {
        return ascii.substring(0, ascii.length - size - 1) + Array(size+1).join('▀');
      }

      return ascii.substring(0, ascii.length-1);
    };

    _this.createASCII = function(cellSize, margin) {
      cellSize = cellSize || 1;

      if (cellSize < 2) {
        return _createHalfASCII(margin);
      }

      cellSize -= 1;
      margin = (typeof margin == 'undefined')? cellSize * 2 : margin;

      var size = _this.getModuleCount() * cellSize + margin * 2;
      var min = margin;
      var max = size - margin;

      var y, x, r, p;

      var white = Array(cellSize+1).join('██');
      var black = Array(cellSize+1).join('  ');

      var ascii = '';
      var line = '';
      for (y = 0; y < size; y += 1) {
        r = Math.floor( (y - min) / cellSize);
        line = '';
        for (x = 0; x < size; x += 1) {
          p = 1;

          if (min <= x && x < max && min <= y && y < max && _this.isDark(r, Math.floor((x - min) / cellSize))) {
            p = 0;
          }

          // Output 2 characters per pixel, to create full square. 1 character per pixels gives only half width of square.
          line += p ? white : black;
        }

        for (r = 0; r < cellSize; r += 1) {
          ascii += line + '\n';
        }
      }

      return ascii.substring(0, ascii.length-1);
    };

    _this.renderTo2dContext = function(context, cellSize) {
      cellSize = cellSize || 2;
      var length = _this.getModuleCount();
      for (var row = 0; row < length; row++) {
        for (var col = 0; col < length; col++) {
          context.fillStyle = _this.isDark(row, col) ? 'black' : 'white';
          context.fillRect(col * cellSize, row * cellSize, cellSize, cellSize);
        }
      }
    }

    return _this;
  };

  //---------------------------------------------------------------------
  // qrcode.stringToBytes
  //---------------------------------------------------------------------

  qrcode.stringToBytesFuncs = {
    'default' : function(s) {
      var bytes = [];
      for (var i = 0; i < s.length; i += 1) {
        var c = s.charCodeAt(i);
        bytes.push(c & 0xff);
      }
      return bytes;
    }
  };

  qrcode.stringToBytes = qrcode.stringToBytesFuncs['default'];

  //---------------------------------------------------------------------
  // qrcode.createStringToBytes
  //---------------------------------------------------------------------

  /**
   * @param unicodeData base64 string of byte array.
   * [16bit Unicode],[16bit Bytes], ...
   * @param numChars
   */
  qrcode.createStringToBytes = function(unicodeData, numChars) {

    // create conversion map.

    var unicodeMap = function() {

      var bin = base64DecodeInputStream(unicodeData);
      var read = function() {
        var b = bin.read();
        if (b == -1) throw 'eof';
        return b;
      };

      var count = 0;
      var unicodeMap = {};
      while (true) {
        var b0 = bin.read();
        if (b0 == -1) break;
        var b1 = read();
        var b2 = read();
        var b3 = read();
        var k = String.fromCharCode( (b0 << 8) | b1);
        var v = (b2 << 8) | b3;
        unicodeMap[k] = v;
        count += 1;
      }
      if (count != numChars) {
        throw count + ' != ' + numChars;
      }

      return unicodeMap;
    }();

    var unknownChar = '?'.charCodeAt(0);

    return function(s) {
      var bytes = [];
      for (var i = 0; i < s.length; i += 1) {
        var c = s.charCodeAt(i);
        if (c < 128) {
          bytes.push(c);
        } else {
          var b = unicodeMap[s.charAt(i)];
          if (typeof b == 'number') {
            if ( (b & 0xff) == b) {
              // 1byte
              bytes.push(b);
            } else {
              // 2bytes
              bytes.push(b >>> 8);
              bytes.push(b & 0xff);
            }
          } else {
            bytes.push(unknownChar);
          }
        }
      }
      return bytes;
    };
  };

  //---------------------------------------------------------------------
  // QRMode
  //---------------------------------------------------------------------

  var QRMode = {
    MODE_NUMBER :    1 << 0,
    MODE_ALPHA_NUM : 1 << 1,
    MODE_8BIT_BYTE : 1 << 2,
    MODE_KANJI :     1 << 3
  };

  //---------------------------------------------------------------------
  // QRErrorCorrectionLevel
  //---------------------------------------------------------------------

  var QRErrorCorrectionLevel = {
    L : 1,
    M : 0,
    Q : 3,
    H : 2
  };

  //---------------------------------------------------------------------
  // QRMaskPattern
  //---------------------------------------------------------------------

  var QRMaskPattern = {
    PATTERN000 : 0,
    PATTERN001 : 1,
    PATTERN010 : 2,
    PATTERN011 : 3,
    PATTERN100 : 4,
    PATTERN101 : 5,
    PATTERN110 : 6,
    PATTERN111 : 7
  };

  //---------------------------------------------------------------------
  // QRUtil
  //---------------------------------------------------------------------

  var QRUtil = function() {

    var PATTERN_POSITION_TABLE = [
      [],
      [6, 18],
      [6, 22],
      [6, 26],
      [6, 30],
      [6, 34],
      [6, 22, 38],
      [6, 24, 42],
      [6, 26, 46],
      [6, 28, 50],
      [6, 30, 54],
      [6, 32, 58],
      [6, 34, 62],
      [6, 26, 46, 66],
      [6, 26, 48, 70],
      [6, 26, 50, 74],
      [6, 30, 54, 78],
      [6, 30, 56, 82],
      [6, 30, 58, 86],
      [6, 34, 62, 90],
      [6, 28, 50, 72, 94],
      [6, 26, 50, 74, 98],
      [6, 30, 54, 78, 102],
      [6, 28, 54, 80, 106],
      [6, 32, 58, 84, 110],
      [6, 30, 58, 86, 114],
      [6, 34, 62, 90, 118],
      [6, 26, 50, 74, 98, 122],
      [6, 30, 54, 78, 102, 126],
      [6, 26, 52, 78, 104, 130],
      [6, 30, 56, 82, 108, 134],
      [6, 34, 60, 86, 112, 138],
      [6, 30, 58, 86, 114, 142],
      [6, 34, 62, 90, 118, 146],
      [6, 30, 54, 78, 102, 126, 150],
      [6, 24, 50, 76, 102, 128, 154],
      [6, 28, 54, 80, 106, 132, 158],
      [6, 32, 58, 84, 110, 136, 162],
      [6, 26, 54, 82, 110, 138, 166],
      [6, 30, 58, 86, 114, 142, 170]
    ];
    var G15 = (1 << 10) | (1 << 8) | (1 << 5) | (1 << 4) | (1 << 2) | (1 << 1) | (1 << 0);
    var G18 = (1 << 12) | (1 << 11) | (1 << 10) | (1 << 9) | (1 << 8) | (1 << 5) | (1 << 2) | (1 << 0);
    var G15_MASK = (1 << 14) | (1 << 12) | (1 << 10) | (1 << 4) | (1 << 1);

    var _this = {};

    var getBCHDigit = function(data) {
      var digit = 0;
      while (data != 0) {
        digit += 1;
        data >>>= 1;
      }
      return digit;
    };

    _this.getBCHTypeInfo = function(data) {
      var d = data << 10;
      while (getBCHDigit(d) - getBCHDigit(G15) >= 0) {
        d ^= (G15 << (getBCHDigit(d) - getBCHDigit(G15) ) );
      }
      return ( (data << 10) | d) ^ G15_MASK;
    };

    _this.getBCHTypeNumber = function(data) {
      var d = data << 12;
      while (getBCHDigit(d) - getBCHDigit(G18) >= 0) {
        d ^= (G18 << (getBCHDigit(d) - getBCHDigit(G18) ) );
      }
      return (data << 12) | d;
    };

    _this.getPatternPosition = function(typeNumber) {
      return PATTERN_POSITION_TABLE[typeNumber - 1];
    };

    _this.getMaskFunction = function(maskPattern) {

      switch (maskPattern) {

      case QRMaskPattern.PATTERN000 :
        return function(i, j) { return (i + j) % 2 == 0; };
      case QRMaskPattern.PATTERN001 :
        return function(i, j) { return i % 2 == 0; };
      case QRMaskPattern.PATTERN010 :
        return function(i, j) { return j % 3 == 0; };
      case QRMaskPattern.PATTERN011 :
        return function(i, j) { return (i + j) % 3 == 0; };
      case QRMaskPattern.PATTERN100 :
        return function(i, j) { return (Math.floor(i / 2) + Math.floor(j / 3) ) % 2 == 0; };
      case QRMaskPattern.PATTERN101 :
        return function(i, j) { return (i * j) % 2 + (i * j) % 3 == 0; };
      case QRMaskPattern.PATTERN110 :
        return function(i, j) { return ( (i * j) % 2 + (i * j) % 3) % 2 == 0; };
      case QRMaskPattern.PATTERN111 :
        return function(i, j) { return ( (i * j) % 3 + (i + j) % 2) % 2 == 0; };

      default :
        throw 'bad maskPattern:' + maskPattern;
      }
    };

    _this.getErrorCorrectPolynomial = function(errorCorrectLength) {
      var a = qrPolynomial([1], 0);
      for (var i = 0; i < errorCorrectLength; i += 1) {
        a = a.multiply(qrPolynomial([1, QRMath.gexp(i)], 0) );
      }
      return a;
    };

    _this.getLengthInBits = function(mode, type) {

      if (1 <= type && type < 10) {

        // 1 - 9

        switch(mode) {
        case QRMode.MODE_NUMBER    : return 10;
        case QRMode.MODE_ALPHA_NUM : return 9;
        case QRMode.MODE_8BIT_BYTE : return 8;
        case QRMode.MODE_KANJI     : return 8;
        default :
          throw 'mode:' + mode;
        }

      } else if (type < 27) {

        // 10 - 26

        switch(mode) {
        case QRMode.MODE_NUMBER    : return 12;
        case QRMode.MODE_ALPHA_NUM : return 11;
        case QRMode.MODE_8BIT_BYTE : return 16;
        case QRMode.MODE_KANJI     : return 10;
        default :
          throw 'mode:' + mode;
        }

      } else if (type < 41) {

        // 27 - 40

        switch(mode) {
        case QRMode.MODE_NUMBER    : return 14;
        case QRMode.MODE_ALPHA_NUM : return 13;
        case QRMode.MODE_8BIT_BYTE : return 16;
        case QRMode.MODE_KANJI     : return 12;
        default :
          throw 'mode:' + mode;
        }

      } else {
        throw 'type:' + type;
      }
    };

    _this.getLostPoint = function(qrcode) {

      var moduleCount = qrcode.getModuleCount();

      var lostPoint = 0;

      // LEVEL1

      for (var row = 0; row < moduleCount; row += 1) {
        for (var col = 0; col < moduleCount; col += 1) {

          var sameCount = 0;
          var dark = qrcode.isDark(row, col);

          for (var r = -1; r <= 1; r += 1) {

            if (row + r < 0 || moduleCount <= row + r) {
              continue;
            }

            for (var c = -1; c <= 1; c += 1) {

              if (col + c < 0 || moduleCount <= col + c) {
                continue;
              }

              if (r == 0 && c == 0) {
                continue;
              }

              if (dark == qrcode.isDark(row + r, col + c) ) {
                sameCount += 1;
              }
            }
          }

          if (sameCount > 5) {
            lostPoint += (3 + sameCount - 5);
          }
        }
      };

      // LEVEL2

      for (var row = 0; row < moduleCount - 1; row += 1) {
        for (var col = 0; col < moduleCount - 1; col += 1) {
          var count = 0;
          if (qrcode.isDark(row, col) ) count += 1;
          if (qrcode.isDark(row + 1, col) ) count += 1;
          if (qrcode.isDark(row, col + 1) ) count += 1;
          if (qrcode.isDark(row + 1, col + 1) ) count += 1;
          if (count == 0 || count == 4) {
            lostPoint += 3;
          }
        }
      }

      // LEVEL3

      for (var row = 0; row < moduleCount; row += 1) {
        for (var col = 0; col < moduleCount - 6; col += 1) {
          if (qrcode.isDark(row, col)
              && !qrcode.isDark(row, col + 1)
              &&  qrcode.isDark(row, col + 2)
              &&  qrcode.isDark(row, col + 3)
              &&  qrcode.isDark(row, col + 4)
              && !qrcode.isDark(row, col + 5)
              &&  qrcode.isDark(row, col + 6) ) {
            lostPoint += 40;
          }
        }
      }

      for (var col = 0; col < moduleCount; col += 1) {
        for (var row = 0; row < moduleCount - 6; row += 1) {
          if (qrcode.isDark(row, col)
              && !qrcode.isDark(row + 1, col)
              &&  qrcode.isDark(row + 2, col)
              &&  qrcode.isDark(row + 3, col)
              &&  qrcode.isDark(row + 4, col)
              && !qrcode.isDark(row + 5, col)
              &&  qrcode.isDark(row + 6, col) ) {
            lostPoint += 40;
          }
        }
      }

      // LEVEL4

      var darkCount = 0;

      for (var col = 0; col < moduleCount; col += 1) {
        for (var row = 0; row < moduleCount; row += 1) {
          if (qrcode.isDark(row, col) ) {
            darkCount += 1;
          }
        }
      }

      var ratio = Math.abs(100 * darkCount / moduleCount / moduleCount - 50) / 5;
      lostPoint += ratio * 10;

      return lostPoint;
    };

    return _this;
  }();

  //---------------------------------------------------------------------
  // QRMath
  //---------------------------------------------------------------------

  var QRMath = function() {

    var EXP_TABLE = new Array(256);
    var LOG_TABLE = new Array(256);

    // initialize tables
    for (var i = 0; i < 8; i += 1) {
      EXP_TABLE[i] = 1 << i;
    }
    for (var i = 8; i < 256; i += 1) {
      EXP_TABLE[i] = EXP_TABLE[i - 4]
        ^ EXP_TABLE[i - 5]
        ^ EXP_TABLE[i - 6]
        ^ EXP_TABLE[i - 8];
    }
    for (var i = 0; i < 255; i += 1) {
      LOG_TABLE[EXP_TABLE[i] ] = i;
    }

    var _this = {};

    _this.glog = function(n) {

      if (n < 1) {
        throw 'glog(' + n + ')';
      }

      return LOG_TABLE[n];
    };

    _this.gexp = function(n) {

      while (n < 0) {
        n += 255;
      }

      while (n >= 256) {
        n -= 255;
      }

      return EXP_TABLE[n];
    };

    return _this;
  }();

  //---------------------------------------------------------------------
  // qrPolynomial
  //---------------------------------------------------------------------

  function qrPolynomial(num, shift) {

    if (typeof num.length == 'undefined') {
      throw num.length + '/' + shift;
    }

    var _num = function() {
      var offset = 0;
      while (offset < num.length && num[offset] == 0) {
        offset += 1;
      }
      var _num = new Array(num.length - offset + shift);
      for (var i = 0; i < num.length - offset; i += 1) {
        _num[i] = num[i + offset];
      }
      return _num;
    }();

    var _this = {};

    _this.getAt = function(index) {
      return _num[index];
    };

    _this.getLength = function() {
      return _num.length;
    };

    _this.multiply = function(e) {

      var num = new Array(_this.getLength() + e.getLength() - 1);

      for (var i = 0; i < _this.getLength(); i += 1) {
        for (var j = 0; j < e.getLength(); j += 1) {
          num[i + j] ^= QRMath.gexp(QRMath.glog(_this.getAt(i) ) + QRMath.glog(e.getAt(j) ) );
        }
      }

      return qrPolynomial(num, 0);
    };

    _this.mod = function(e) {

      if (_this.getLength() - e.getLength() < 0) {
        return _this;
      }

      var ratio = QRMath.glog(_this.getAt(0) ) - QRMath.glog(e.getAt(0) );

      var num = new Array(_this.getLength() );
      for (var i = 0; i < _this.getLength(); i += 1) {
        num[i] = _this.getAt(i);
      }

      for (var i = 0; i < e.getLength(); i += 1) {
        num[i] ^= QRMath.gexp(QRMath.glog(e.getAt(i) ) + ratio);
      }

      // recursive call
      return qrPolynomial(num, 0).mod(e);
    };

    return _this;
  };

  //---------------------------------------------------------------------
  // QRRSBlock
  //---------------------------------------------------------------------

  var QRRSBlock = function() {

    var RS_BLOCK_TABLE = [

      // L
      // M
      // Q
      // H

      // 1
      [1, 26, 19],
      [1, 26, 16],
      [1, 26, 13],
      [1, 26, 9],

      // 2
      [1, 44, 34],
      [1, 44, 28],
      [1, 44, 22],
      [1, 44, 16],

      // 3
      [1, 70, 55],
      [1, 70, 44],
      [2, 35, 17],
      [2, 35, 13],

      // 4
      [1, 100, 80],
      [2, 50, 32],
      [2, 50, 24],
      [4, 25, 9],

      // 5
      [1, 134, 108],
      [2, 67, 43],
      [2, 33, 15, 2, 34, 16],
      [2, 33, 11, 2, 34, 12],

      // 6
      [2, 86, 68],
      [4, 43, 27],
      [4, 43, 19],
      [4, 43, 15],

      // 7
      [2, 98, 78],
      [4, 49, 31],
      [2, 32, 14, 4, 33, 15],
      [4, 39, 13, 1, 40, 14],

      // 8
      [2, 121, 97],
      [2, 60, 38, 2, 61, 39],
      [4, 40, 18, 2, 41, 19],
      [4, 40, 14, 2, 41, 15],

      // 9
      [2, 146, 116],
      [3, 58, 36, 2, 59, 37],
      [4, 36, 16, 4, 37, 17],
      [4, 36, 12, 4, 37, 13],

      // 10
      [2, 86, 68, 2, 87, 69],
      [4, 69, 43, 1, 70, 44],
      [6, 43, 19, 2, 44, 20],
      [6, 43, 15, 2, 44, 16],

      // 11
      [4, 101, 81],
      [1, 80, 50, 4, 81, 51],
      [4, 50, 22, 4, 51, 23],
      [3, 36, 12, 8, 37, 13],

      // 12
      [2, 116, 92, 2, 117, 93],
      [6, 58, 36, 2, 59, 37],
      [4, 46, 20, 6, 47, 21],
      [7, 42, 14, 4, 43, 15],

      // 13
      [4, 133, 107],
      [8, 59, 37, 1, 60, 38],
      [8, 44, 20, 4, 45, 21],
      [12, 33, 11, 4, 34, 12],

      // 14
      [3, 145, 115, 1, 146, 116],
      [4, 64, 40, 5, 65, 41],
      [11, 36, 16, 5, 37, 17],
      [11, 36, 12, 5, 37, 13],

      // 15
      [5, 109, 87, 1, 110, 88],
      [5, 65, 41, 5, 66, 42],
      [5, 54, 24, 7, 55, 25],
      [11, 36, 12, 7, 37, 13],

      // 16
      [5, 122, 98, 1, 123, 99],
      [7, 73, 45, 3, 74, 46],
      [15, 43, 19, 2, 44, 20],
      [3, 45, 15, 13, 46, 16],

      // 17
      [1, 135, 107, 5, 136, 108],
      [10, 74, 46, 1, 75, 47],
      [1, 50, 22, 15, 51, 23],
      [2, 42, 14, 17, 43, 15],

      // 18
      [5, 150, 120, 1, 151, 121],
      [9, 69, 43, 4, 70, 44],
      [17, 50, 22, 1, 51, 23],
      [2, 42, 14, 19, 43, 15],

      // 19
      [3, 141, 113, 4, 142, 114],
      [3, 70, 44, 11, 71, 45],
      [17, 47, 21, 4, 48, 22],
      [9, 39, 13, 16, 40, 14],

      // 20
      [3, 135, 107, 5, 136, 108],
      [3, 67, 41, 13, 68, 42],
      [15, 54, 24, 5, 55, 25],
      [15, 43, 15, 10, 44, 16],

      // 21
      [4, 144, 116, 4, 145, 117],
      [17, 68, 42],
      [17, 50, 22, 6, 51, 23],
      [19, 46, 16, 6, 47, 17],

      // 22
      [2, 139, 111, 7, 140, 112],
      [17, 74, 46],
      [7, 54, 24, 16, 55, 25],
      [34, 37, 13],

      // 23
      [4, 151, 121, 5, 152, 122],
      [4, 75, 47, 14, 76, 48],
      [11, 54, 24, 14, 55, 25],
      [16, 45, 15, 14, 46, 16],

      // 24
      [6, 147, 117, 4, 148, 118],
      [6, 73, 45, 14, 74, 46],
      [11, 54, 24, 16, 55, 25],
      [30, 46, 16, 2, 47, 17],

      // 25
      [8, 132, 106, 4, 133, 107],
      [8, 75, 47, 13, 76, 48],
      [7, 54, 24, 22, 55, 25],
      [22, 45, 15, 13, 46, 16],

      // 26
      [10, 142, 114, 2, 143, 115],
      [19, 74, 46, 4, 75, 47],
      [28, 50, 22, 6, 51, 23],
      [33, 46, 16, 4, 47, 17],

      // 27
      [8, 152, 122, 4, 153, 123],
      [22, 73, 45, 3, 74, 46],
      [8, 53, 23, 26, 54, 24],
      [12, 45, 15, 28, 46, 16],

      // 28
      [3, 147, 117, 10, 148, 118],
      [3, 73, 45, 23, 74, 46],
      [4, 54, 24, 31, 55, 25],
      [11, 45, 15, 31, 46, 16],

      // 29
      [7, 146, 116, 7, 147, 117],
      [21, 73, 45, 7, 74, 46],
      [1, 53, 23, 37, 54, 24],
      [19, 45, 15, 26, 46, 16],

      // 30
      [5, 145, 115, 10, 146, 116],
      [19, 75, 47, 10, 76, 48],
      [15, 54, 24, 25, 55, 25],
      [23, 45, 15, 25, 46, 16],

      // 31
      [13, 145, 115, 3, 146, 116],
      [2, 74, 46, 29, 75, 47],
      [42, 54, 24, 1, 55, 25],
      [23, 45, 15, 28, 46, 16],

      // 32
      [17, 145, 115],
      [10, 74, 46, 23, 75, 47],
      [10, 54, 24, 35, 55, 25],
      [19, 45, 15, 35, 46, 16],

      // 33
      [17, 145, 115, 1, 146, 116],
      [14, 74, 46, 21, 75, 47],
      [29, 54, 24, 19, 55, 25],
      [11, 45, 15, 46, 46, 16],

      // 34
      [13, 145, 115, 6, 146, 116],
      [14, 74, 46, 23, 75, 47],
      [44, 54, 24, 7, 55, 25],
      [59, 46, 16, 1, 47, 17],

      // 35
      [12, 151, 121, 7, 152, 122],
      [12, 75, 47, 26, 76, 48],
      [39, 54, 24, 14, 55, 25],
      [22, 45, 15, 41, 46, 16],

      // 36
      [6, 151, 121, 14, 152, 122],
      [6, 75, 47, 34, 76, 48],
      [46, 54, 24, 10, 55, 25],
      [2, 45, 15, 64, 46, 16],

      // 37
      [17, 152, 122, 4, 153, 123],
      [29, 74, 46, 14, 75, 47],
      [49, 54, 24, 10, 55, 25],
      [24, 45, 15, 46, 46, 16],

      // 38
      [4, 152, 122, 18, 153, 123],
      [13, 74, 46, 32, 75, 47],
      [48, 54, 24, 14, 55, 25],
      [42, 45, 15, 32, 46, 16],

      // 39
      [20, 147, 117, 4, 148, 118],
      [40, 75, 47, 7, 76, 48],
      [43, 54, 24, 22, 55, 25],
      [10, 45, 15, 67, 46, 16],

      // 40
      [19, 148, 118, 6, 149, 119],
      [18, 75, 47, 31, 76, 48],
      [34, 54, 24, 34, 55, 25],
      [20, 45, 15, 61, 46, 16]
    ];

    var qrRSBlock = function(totalCount, dataCount) {
      var _this = {};
      _this.totalCount = totalCount;
      _this.dataCount = dataCount;
      return _this;
    };

    var _this = {};

    var getRsBlockTable = function(typeNumber, errorCorrectionLevel) {

      switch(errorCorrectionLevel) {
      case QRErrorCorrectionLevel.L :
        return RS_BLOCK_TABLE[(typeNumber - 1) * 4 + 0];
      case QRErrorCorrectionLevel.M :
        return RS_BLOCK_TABLE[(typeNumber - 1) * 4 + 1];
      case QRErrorCorrectionLevel.Q :
        return RS_BLOCK_TABLE[(typeNumber - 1) * 4 + 2];
      case QRErrorCorrectionLevel.H :
        return RS_BLOCK_TABLE[(typeNumber - 1) * 4 + 3];
      default :
        return undefined;
      }
    };

    _this.getRSBlocks = function(typeNumber, errorCorrectionLevel) {

      var rsBlock = getRsBlockTable(typeNumber, errorCorrectionLevel);

      if (typeof rsBlock == 'undefined') {
        throw 'bad rs block @ typeNumber:' + typeNumber +
            '/errorCorrectionLevel:' + errorCorrectionLevel;
      }

      var length = rsBlock.length / 3;

      var list = [];

      for (var i = 0; i < length; i += 1) {

        var count = rsBlock[i * 3 + 0];
        var totalCount = rsBlock[i * 3 + 1];
        var dataCount = rsBlock[i * 3 + 2];

        for (var j = 0; j < count; j += 1) {
          list.push(qrRSBlock(totalCount, dataCount) );
        }
      }

      return list;
    };

    return _this;
  }();

  //---------------------------------------------------------------------
  // qrBitBuffer
  //---------------------------------------------------------------------

  var qrBitBuffer = function() {

    var _buffer = [];
    var _length = 0;

    var _this = {};

    _this.getBuffer = function() {
      return _buffer;
    };

    _this.getAt = function(index) {
      var bufIndex = Math.floor(index / 8);
      return ( (_buffer[bufIndex] >>> (7 - index % 8) ) & 1) == 1;
    };

    _this.put = function(num, length) {
      for (var i = 0; i < length; i += 1) {
        _this.putBit( ( (num >>> (length - i - 1) ) & 1) == 1);
      }
    };

    _this.getLengthInBits = function() {
      return _length;
    };

    _this.putBit = function(bit) {

      var bufIndex = Math.floor(_length / 8);
      if (_buffer.length <= bufIndex) {
        _buffer.push(0);
      }

      if (bit) {
        _buffer[bufIndex] |= (0x80 >>> (_length % 8) );
      }

      _length += 1;
    };

    return _this;
  };

  //---------------------------------------------------------------------
  // qrNumber
  //---------------------------------------------------------------------

  var qrNumber = function(data) {

    var _mode = QRMode.MODE_NUMBER;
    var _data = data;

    var _this = {};

    _this.getMode = function() {
      return _mode;
    };

    _this.getLength = function(buffer) {
      return _data.length;
    };

    _this.write = function(buffer) {

      var data = _data;

      var i = 0;

      while (i + 2 < data.length) {
        buffer.put(strToNum(data.substring(i, i + 3) ), 10);
        i += 3;
      }

      if (i < data.length) {
        if (data.length - i == 1) {
          buffer.put(strToNum(data.substring(i, i + 1) ), 4);
        } else if (data.length - i == 2) {
          buffer.put(strToNum(data.substring(i, i + 2) ), 7);
        }
      }
    };

    var strToNum = function(s) {
      var num = 0;
      for (var i = 0; i < s.length; i += 1) {
        num = num * 10 + chatToNum(s.charAt(i) );
      }
      return num;
    };

    var chatToNum = function(c) {
      if ('0' <= c && c <= '9') {
        return c.charCodeAt(0) - '0'.charCodeAt(0);
      }
      throw 'illegal char :' + c;
    };

    return _this;
  };

  //---------------------------------------------------------------------
  // qrAlphaNum
  //---------------------------------------------------------------------

  var qrAlphaNum = function(data) {

    var _mode = QRMode.MODE_ALPHA_NUM;
    var _data = data;

    var _this = {};

    _this.getMode = function() {
      return _mode;
    };

    _this.getLength = function(buffer) {
      return _data.length;
    };

    _this.write = function(buffer) {

      var s = _data;

      var i = 0;

      while (i + 1 < s.length) {
        buffer.put(
          getCode(s.charAt(i) ) * 45 +
          getCode(s.charAt(i + 1) ), 11);
        i += 2;
      }

      if (i < s.length) {
        buffer.put(getCode(s.charAt(i) ), 6);
      }
    };

    var getCode = function(c) {

      if ('0' <= c && c <= '9') {
        return c.charCodeAt(0) - '0'.charCodeAt(0);
      } else if ('A' <= c && c <= 'Z') {
        return c.charCodeAt(0) - 'A'.charCodeAt(0) + 10;
      } else {
        switch (c) {
        case ' ' : return 36;
        case '$' : return 37;
        case '%' : return 38;
        case '*' : return 39;
        case '+' : return 40;
        case '-' : return 41;
        case '.' : return 42;
        case '/' : return 43;
        case ':' : return 44;
        default :
          throw 'illegal char :' + c;
        }
      }
    };

    return _this;
  };

  //---------------------------------------------------------------------
  // qr8BitByte
  //---------------------------------------------------------------------

  var qr8BitByte = function(data) {

    var _mode = QRMode.MODE_8BIT_BYTE;
    var _data = data;
    var _bytes = qrcode.stringToBytes(data);

    var _this = {};

    _this.getMode = function() {
      return _mode;
    };

    _this.getLength = function(buffer) {
      return _bytes.length;
    };

    _this.write = function(buffer) {
      for (var i = 0; i < _bytes.length; i += 1) {
        buffer.put(_bytes[i], 8);
      }
    };

    return _this;
  };

  //---------------------------------------------------------------------
  // qrKanji
  //---------------------------------------------------------------------

  var qrKanji = function(data) {

    var _mode = QRMode.MODE_KANJI;
    var _data = data;

    var stringToBytes = qrcode.stringToBytesFuncs['SJIS'];
    if (!stringToBytes) {
      throw 'sjis not supported.';
    }
    !function(c, code) {
      // self test for sjis support.
      var test = stringToBytes(c);
      if (test.length != 2 || ( (test[0] << 8) | test[1]) != code) {
        throw 'sjis not supported.';
      }
    }('\u53cb', 0x9746);

    var _bytes = stringToBytes(data);

    var _this = {};

    _this.getMode = function() {
      return _mode;
    };

    _this.getLength = function(buffer) {
      return ~~(_bytes.length / 2);
    };

    _this.write = function(buffer) {

      var data = _bytes;

      var i = 0;

      while (i + 1 < data.length) {

        var c = ( (0xff & data[i]) << 8) | (0xff & data[i + 1]);

        if (0x8140 <= c && c <= 0x9FFC) {
          c -= 0x8140;
        } else if (0xE040 <= c && c <= 0xEBBF) {
          c -= 0xC140;
        } else {
          throw 'illegal char at ' + (i + 1) + '/' + c;
        }

        c = ( (c >>> 8) & 0xff) * 0xC0 + (c & 0xff);

        buffer.put(c, 13);

        i += 2;
      }

      if (i < data.length) {
        throw 'illegal char at ' + (i + 1);
      }
    };

    return _this;
  };

  //=====================================================================
  // GIF Support etc.
  //

  //---------------------------------------------------------------------
  // byteArrayOutputStream
  //---------------------------------------------------------------------

  var byteArrayOutputStream = function() {

    var _bytes = [];

    var _this = {};

    _this.writeByte = function(b) {
      _bytes.push(b & 0xff);
    };

    _this.writeShort = function(i) {
      _this.writeByte(i);
      _this.writeByte(i >>> 8);
    };

    _this.writeBytes = function(b, off, len) {
      off = off || 0;
      len = len || b.length;
      for (var i = 0; i < len; i += 1) {
        _this.writeByte(b[i + off]);
      }
    };

    _this.writeString = function(s) {
      for (var i = 0; i < s.length; i += 1) {
        _this.writeByte(s.charCodeAt(i) );
      }
    };

    _this.toByteArray = function() {
      return _bytes;
    };

    _this.toString = function() {
      var s = '';
      s += '[';
      for (var i = 0; i < _bytes.length; i += 1) {
        if (i > 0) {
          s += ',';
        }
        s += _bytes[i];
      }
      s += ']';
      return s;
    };

    return _this;
  };

  //---------------------------------------------------------------------
  // base64EncodeOutputStream
  //---------------------------------------------------------------------

  var base64EncodeOutputStream = function() {

    var _buffer = 0;
    var _buflen = 0;
    var _length = 0;
    var _base64 = '';

    var _this = {};

    var writeEncoded = function(b) {
      _base64 += String.fromCharCode(encode(b & 0x3f) );
    };

    var encode = function(n) {
      if (n < 0) {
        // error.
      } else if (n < 26) {
        return 0x41 + n;
      } else if (n < 52) {
        return 0x61 + (n - 26);
      } else if (n < 62) {
        return 0x30 + (n - 52);
      } else if (n == 62) {
        return 0x2b;
      } else if (n == 63) {
        return 0x2f;
      }
      throw 'n:' + n;
    };

    _this.writeByte = function(n) {

      _buffer = (_buffer << 8) | (n & 0xff);
      _buflen += 8;
      _length += 1;

      while (_buflen >= 6) {
        writeEncoded(_buffer >>> (_buflen - 6) );
        _buflen -= 6;
      }
    };

    _this.flush = function() {

      if (_buflen > 0) {
        writeEncoded(_buffer << (6 - _buflen) );
        _buffer = 0;
        _buflen = 0;
      }

      if (_length % 3 != 0) {
        // padding
        var padlen = 3 - _length % 3;
        for (var i = 0; i < padlen; i += 1) {
          _base64 += '=';
        }
      }
    };

    _this.toString = function() {
      return _base64;
    };

    return _this;
  };

  //---------------------------------------------------------------------
  // base64DecodeInputStream
  //---------------------------------------------------------------------

  var base64DecodeInputStream = function(str) {

    var _str = str;
    var _pos = 0;
    var _buffer = 0;
    var _buflen = 0;

    var _this = {};

    _this.read = function() {

      while (_buflen < 8) {

        if (_pos >= _str.length) {
          if (_buflen == 0) {
            return -1;
          }
          throw 'unexpected end of file./' + _buflen;
        }

        var c = _str.charAt(_pos);
        _pos += 1;

        if (c == '=') {
          _buflen = 0;
          return -1;
        } else if (c.match(/^\s$/) ) {
          // ignore if whitespace.
          continue;
        }

        _buffer = (_buffer << 6) | decode(c.charCodeAt(0) );
        _buflen += 6;
      }

      var n = (_buffer >>> (_buflen - 8) ) & 0xff;
      _buflen -= 8;
      return n;
    };

    var decode = function(c) {
      if (0x41 <= c && c <= 0x5a) {
        return c - 0x41;
      } else if (0x61 <= c && c <= 0x7a) {
        return c - 0x61 + 26;
      } else if (0x30 <= c && c <= 0x39) {
        return c - 0x30 + 52;
      } else if (c == 0x2b) {
        return 62;
      } else if (c == 0x2f) {
        return 63;
      } else {
        throw 'c:' + c;
      }
    };

    return _this;
  };

  //---------------------------------------------------------------------
  // gifImage (B/W)
  //---------------------------------------------------------------------

  var gifImage = function(width, height) {

    var _width = width;
    var _height = height;
    var _data = new Array(width * height);

    var _this = {};

    _this.setPixel = function(x, y, pixel) {
      _data[y * _width + x] = pixel;
    };

    _this.write = function(out) {

      //---------------------------------
      // GIF Signature

      out.writeString('GIF87a');

      //---------------------------------
      // Screen Descriptor

      out.writeShort(_width);
      out.writeShort(_height);

      out.writeByte(0x80); // 2bit
      out.writeByte(0);
      out.writeByte(0);

      //---------------------------------
      // Global Color Map

      // black
      out.writeByte(0x00);
      out.writeByte(0x00);
      out.writeByte(0x00);

      // white
      out.writeByte(0xff);
      out.writeByte(0xff);
      out.writeByte(0xff);

      //---------------------------------
      // Image Descriptor

      out.writeString(',');
      out.writeShort(0);
      out.writeShort(0);
      out.writeShort(_width);
      out.writeShort(_height);
      out.writeByte(0);

      //---------------------------------
      // Local Color Map

      //---------------------------------
      // Raster Data

      var lzwMinCodeSize = 2;
      var raster = getLZWRaster(lzwMinCodeSize);

      out.writeByte(lzwMinCodeSize);

      var offset = 0;

      while (raster.length - offset > 255) {
        out.writeByte(255);
        out.writeBytes(raster, offset, 255);
        offset += 255;
      }

      out.writeByte(raster.length - offset);
      out.writeBytes(raster, offset, raster.length - offset);
      out.writeByte(0x00);

      //---------------------------------
      // GIF Terminator
      out.writeString(';');
    };

    var bitOutputStream = function(out) {

      var _out = out;
      var _bitLength = 0;
      var _bitBuffer = 0;

      var _this = {};

      _this.write = function(data, length) {

        if ( (data >>> length) != 0) {
          throw 'length over';
        }

        while (_bitLength + length >= 8) {
          _out.writeByte(0xff & ( (data << _bitLength) | _bitBuffer) );
          length -= (8 - _bitLength);
          data >>>= (8 - _bitLength);
          _bitBuffer = 0;
          _bitLength = 0;
        }

        _bitBuffer = (data << _bitLength) | _bitBuffer;
        _bitLength = _bitLength + length;
      };

      _this.flush = function() {
        if (_bitLength > 0) {
          _out.writeByte(_bitBuffer);
        }
      };

      return _this;
    };

    var getLZWRaster = function(lzwMinCodeSize) {

      var clearCode = 1 << lzwMinCodeSize;
      var endCode = (1 << lzwMinCodeSize) + 1;
      var bitLength = lzwMinCodeSize + 1;

      // Setup LZWTable
      var table = lzwTable();

      for (var i = 0; i < clearCode; i += 1) {
        table.add(String.fromCharCode(i) );
      }
      table.add(String.fromCharCode(clearCode) );
      table.add(String.fromCharCode(endCode) );

      var byteOut = byteArrayOutputStream();
      var bitOut = bitOutputStream(byteOut);

      // clear code
      bitOut.write(clearCode, bitLength);

      var dataIndex = 0;

      var s = String.fromCharCode(_data[dataIndex]);
      dataIndex += 1;

      while (dataIndex < _data.length) {

        var c = String.fromCharCode(_data[dataIndex]);
        dataIndex += 1;

        if (table.contains(s + c) ) {

          s = s + c;

        } else {

          bitOut.write(table.indexOf(s), bitLength);

          if (table.size() < 0xfff) {

            if (table.size() == (1 << bitLength) ) {
              bitLength += 1;
            }

            table.add(s + c);
          }

          s = c;
        }
      }

      bitOut.write(table.indexOf(s), bitLength);

      // end code
      bitOut.write(endCode, bitLength);

      bitOut.flush();

      return byteOut.toByteArray();
    };

    var lzwTable = function() {

      var _map = {};
      var _size = 0;

      var _this = {};

      _this.add = function(key) {
        if (_this.contains(key) ) {
          throw 'dup key:' + key;
        }
        _map[key] = _size;
        _size += 1;
      };

      _this.size = function() {
        return _size;
      };

      _this.indexOf = function(key) {
        return _map[key];
      };

      _this.contains = function(key) {
        return typeof _map[key] != 'undefined';
      };

      return _this;
    };

    return _this;
  };

  var createDataURL = function(width, height, getPixel) {
    var gif = gifImage(width, height);
    for (var y = 0; y < height; y += 1) {
      for (var x = 0; x < width; x += 1) {
        gif.setPixel(x, y, getPixel(x, y) );
      }
    }

    var b = byteArrayOutputStream();
    gif.write(b);

    var base64 = base64EncodeOutputStream();
    var bytes = b.toByteArray();
    for (var i = 0; i < bytes.length; i += 1) {
      base64.writeByte(bytes[i]);
    }
    base64.flush();

    return 'data:image/gif;base64,' + base64;
  };

  //---------------------------------------------------------------------
  // returns qrcode function.

  return qrcode;
}();

// multibyte support
!function() {

  qrcode.stringToBytesFuncs['UTF-8'] = function(s) {
    // http://stackoverflow.com/questions/18729405/how-to-convert-utf8-string-to-byte-array
    function toUTF8Array(str) {
      var utf8 = [];
      for (var i=0; i < str.length; i++) {
        var charcode = str.charCodeAt(i);
        if (charcode < 0x80) utf8.push(charcode);
        else if (charcode < 0x800) {
          utf8.push(0xc0 | (charcode >> 6),
              0x80 | (charcode & 0x3f));
        }
        else if (charcode < 0xd800 || charcode >= 0xe000) {
          utf8.push(0xe0 | (charcode >> 12),
              0x80 | ((charcode>>6) & 0x3f),
              0x80 | (charcode & 0x3f));
        }
        // surrogate pair
        else {
          i++;
          // UTF-16 encodes 0x10000-0x10FFFF by
          // subtracting 0x10000 and splitting the
          // 20 bits of 0x0-0xFFFFF into two halves
          charcode = 0x10000 + (((charcode & 0x3ff)<<10)
            | (str.charCodeAt(i) & 0x3ff));
          utf8.push(0xf0 | (charcode >>18),
              0x80 | ((charcode>>12) & 0x3f),
              0x80 | ((charcode>>6) & 0x3f),
              0x80 | (charcode & 0x3f));
        }
      }
      return utf8;
    }
    return toUTF8Array(s);
  };

}();

function validClientList(data){
 function object(value){return value&&typeof value==='object'&&!Array.isArray(value);}
 if(!object(data)||data.schema!==1||!Array.isArray(data.clients)||data.clients.length>32||!object(data.capabilities))return false;
 var ids={},states=['draft','queued','registering','enrolling','registered','refreshing','selecting_exit','publishing_subscription','stopping_subscription','revoking','revoked','failed','unknown','review'];
 return data.clients.every(function(row){
  if(!object(row)||typeof row.id!=='string'||!/^[a-f0-9]{32}$/.test(row.id)||ids[row.id]||typeof row.name!=='string'||!row.name.trim()||Array.from(row.name).length>64||typeof row.organization!=='string'||!/^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/.test(row.organization)||states.indexOf(row.state)<0||typeof row.label!=='string'||typeof row.busy!=='boolean'||typeof row.has_registration!=='boolean'||typeof row.exportable!=='boolean')return false;
  ids[row.id]=true;
  var cfg=row.settings;
  if(!object(cfg)||Object.keys(cfg).some(function(k){return ['endpoint','port','dns','mtu','keepalive','cc'].indexOf(k)<0;})||typeof cfg.endpoint!=='string'||typeof cfg.dns!=='string'||cfg.dns.length>64||[443,500,1701,4500,4443,8443,8095].indexOf(cfg.port)<0||!Number.isInteger(cfg.mtu)||cfg.mtu<1280||cfg.mtu>1420||!Number.isInteger(cfg.keepalive)||cfg.keepalive<10||cfg.keepalive>120||['cubic','bbr','new_reno'].indexOf(cfg.cc)<0)return false;
  if(row.subscription!=null&&(!object(row.subscription)||['none','prepared','publishing','published','unknown','stopping','disabled'].indexOf(row.subscription.state)<0||typeof row.subscription.enabled!=='boolean'||typeof row.subscription.needs_update!=='boolean'||!Number.isFinite(row.subscription.synced_at)||row.subscription.error!=null&&typeof row.subscription.error!=='string'))return false;
  if(row.network!=null){
   var network=row.network,uuid=/^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/,seen={};
   if(!object(network)||['unread','confirmed','pending','unknown','error'].indexOf(network.state)<0||!Array.isArray(network.networks)||network.networks.length>128||typeof network.selected_id!=='string'||network.selected_id&&!uuid.test(network.selected_id)||typeof network.selected_name!=='string'||!Number.isFinite(network.checked_at)||!network.networks.every(function(n){if(!object(n)||!uuid.test(n.id)||seen[n.id]||typeof n.name!=='string'||!n.name.trim()||n.name.length>128||typeof n.is_default!=='boolean')return false;seen[n.id]=true;return true;})||network.state==='confirmed'&&!seen[network.selected_id]||network.error!=null&&typeof network.error!=='string')return false;
   if(row.exportable&&(network.state!=='confirmed'||!network.networks.some(function(n){return n.id===network.selected_id&&!n.is_default;})))return false;
  }
  if(row.exportable&&(row.state!=='registered'||!row.has_registration))return false;
  return ['ipv4','ipv6','error','fingerprint','registration_id','bootstrap_fingerprint'].every(function(k){return row[k]==null||typeof row[k]==='string';});
 });
}

return baseclass.extend({
 load: function() {
  return fs.exec('/usr/libexec/routepilot-clients-list',[]).then(function(r){var data;try{data=JSON.parse(r.stdout);}catch(e){throw Error('客户端列表响应无效');}if(!data||typeof data!=='object'||Array.isArray(data))throw Error('客户端列表响应无效');if(r.code||data.error)throw Error(data.error||'无法读取客户端列表');if(!validClientList(data))throw Error('客户端列表格式无效');return data;}).catch(function(e){return {clients:[],capabilities:{},load_error:e.message||'读取客户端列表失败'};});
 },
 render: function(initial,stylesheet,bindRequest) {
  var self=this,data=initial,selected=null,inFlight=false,refreshing=false,refreshSerial=0,prepared=false,exportCleanup=null,formSerial=0,drafts={},exitDrafts={};
  var list=E('div',{class:'imok-client-list'}),detail=E('div',{class:'imok-client-detail'}),notice=E('p',{class:'imok-client-notice',role:'status','aria-live':'polite',hidden:true});
  var root=E('div',{class:'rp-layout','data-page':'clients'},[E('link',{rel:'stylesheet',href:stylesheet}),E('div',{class:'rp-header imok-client-header'},[
   E('div',{class:'imok-client-heading'},[E('div',{class:'imok-client-title'},[E('span',{class:'rp-brand'},'imOK'),E('h2',{},'客户端接入')]),E('p',{class:'rp-caption'},'管理手机等设备的独立身份与出口，不影响路由器主线路、DNS 和分流。')]),
   E('div',{class:'rp-backup-actions'},[E('button',{type:'button',class:'btn',click:function(){refresh(true);}},'刷新列表'),E('button',{type:'button',class:'btn cbi-button-action',click:function(){newClient();}},'新增客户端')])
  ]),E('div',{class:'rp-page-content'},[notice,
   E('div',{class:'imok-client-workspace'},[E('section',{class:'imok-panel','aria-label':'客户端列表'},[E('div',{class:'imok-panel-heading'},[E('h3',{},'我的客户端'),E('p',{class:'rp-help'},'只显示本模块创建的身份')]),list]),detail])])]);
  function message(text,error){notice.hidden=false;notice.textContent=text;notice.setAttribute('data-tone',error?'error':'info');}
  function panel(title,description,children){return E('section',{class:'imok-panel'},[E('div',{class:'imok-panel-heading'},[E('h3',{},title),description?E('p',{class:'rp-help'},description):'']),E('div',{class:'imok-panel-body'},children)]);}
  function field(label,input,help){var id='imok-client-field-'+(++formSerial);input.id=id;return E('div',{class:'imok-client-field'},[E('label',{'for':id},label),input,help?E('small',{class:'rp-help'},help):'']);}
  function button(label,action,primary){return E('button',{type:'button',class:'btn'+(primary?' cbi-button-action':''),click:action},label);}
  function modal(title,children){if(exportCleanup)exportCleanup();return ui.showModal(title,children,'imok-dialog','imok-client-dialog');}
  function modalActions(buttons){return E('div',{class:'rp-backup-actions right'},buttons);}
  function currentModal(node){return root.isConnected&&node.isConnected&&node.getClientRects().length>0&&!!node.closest('.imok-client-dialog')&&document.body.classList.contains('modal-overlay-active');}
  function dismiss(node){if(currentModal(node))ui.hideModal();}
  function dirty(row){var draft=drafts[row.id];return !!draft&&['endpoint','port','dns','mtu','keepalive','cc'].some(function(k){return String(draft[k])!==String(row.settings[k]);});}
  function modalError(node,error){node.hidden=false;node.textContent=error.message||'操作失败，请刷新核对';}
  function ensurePrepared(){if(prepared)return Promise.resolve();return fs.exec('/usr/libexec/routepilot-clients',['prepare']).then(function(r){var v;try{v=JSON.parse(r.stdout);}catch(e){throw Error('客户端请求目录未能准备');}if(!v||typeof v!=='object'||Array.isArray(v))throw Error('客户端请求目录响应无效');if(r.code||!v.ready)throw Error(v.error||'没有客户端管理权限');prepared=true;});}
  function request(action,payload){
   if(inFlight)return Promise.reject(Error('已有操作正在提交，请稍候'));
   var acknowledged=false,disabled=[];inFlight=true;if(['export','subscription_link'].indexOf(action)<0)refreshSerial++;root.setAttribute('aria-busy','true');root.querySelectorAll('button,input,select,textarea').forEach(function(b){disabled.push([b,b.disabled]);b.disabled=true;});
   return ensurePrepared().then(function(){var req=bindRequest(payload);return fs.write('/var/run/routepilot/requests/client-input-'+req.id+'.json',req.text,384).then(function(){return fs.exec('/usr/libexec/routepilot-clients',[action,req.token]);}).then(function(r){var v;try{v=JSON.parse(r.stdout);}catch(e){throw Error('未收到有效操作结果，请刷新核对，勿重复注册');}if(!v||typeof v!=='object'||Array.isArray(v))throw Error('客户端操作响应无效，请刷新核对');if(r.code||v.error)throw Error(v.error||'操作未完成');if(v.request_id!==req.id)throw Error('操作结果与当前请求不匹配，请刷新核对');acknowledged=true;return v;});}).finally(function(){inFlight=false;root.removeAttribute('aria-busy');disabled.forEach(function(item){item[0].disabled=item[1];});if(!acknowledged&&['register','refresh','select_exit','publish_subscription','stop_subscription','revoke'].indexOf(action)>=0&&root.isConnected)refresh(false);});
  }
  function refresh(manual){if(!root.isConnected||refreshing||inFlight)return Promise.resolve();var serial=refreshSerial;refreshing=true;return self.load().then(function(v){if(!root.isConnected||serial!==refreshSerial)return;if(v.load_error){data=v;draw();return;}data=v;if(manual)message('客户端列表已刷新。');draw();}).catch(function(e){if(root.isConnected&&serial===refreshSerial)message(e.message,true);}).finally(function(){refreshing=false;if(root.isConnected&&serial!==refreshSerial&&!inFlight)return refresh(false);});}
  function newClient(){
   var name=E('input',{type:'text',required:true,maxlength:64,autocomplete:'off',placeholder:'例如：我的 iPhone'}),org=E('input',{type:'text',required:true,pattern:'[A-Za-z0-9](?:[A-Za-z0-9\\-]{0,61}[A-Za-z0-9])?',maxlength:63,autocomplete:'off',placeholder:'团队名称'}),error=E('p',{hidden:true,role:'alert',class:'imok-client-error'});
   var submit=button('创建客户端',function(){if(!name.reportValidity()||!org.reportValidity())return;submit.disabled=true;request('create',{name:name.value,organization:org.value}).then(function(v){dismiss(submit);if(!root.isConnected)return;selected=v.client.id;message('本地客户端记录已创建，下一步完成官方授权；尚未向云端注册。');return refresh();}).catch(function(e){modalError(error,e);}).finally(function(){submit.disabled=false;});},true);
   modal('新增客户端',[E('p',{class:'rp-help'},'每台设备单独创建身份。同一手机无需仅为不同端口重复注册。'),field('客户端名称',name),field('Cloudflare 团队名称',org,'只填写团队名称，不填写网址。不会修改路由器的团队设置。'),error,modalActions([button('取消',function(){ui.hideModal();}),submit])]);
  }
  function registerClient(row){
   var token=E('textarea',{rows:3,autocomplete:'off',spellcheck:'false','aria-label':'官方登录授权链接'}),terms=E('input',{type:'checkbox'}),error=E('p',{hidden:true,role:'alert',class:'imok-client-error'}),url='https://'+row.organization+'.cloudflareaccess.com/warp';
   var submit=button('注册独立客户端',function(){var value=token.value;token.value='';submit.disabled=true;request('register',{id:row.id,token:value,accept_terms:terms.checked}).then(function(){dismiss(submit);if(!root.isConnected)return;message('独立注册任务已提交，等待云端确认；主线路保持原状。');return refresh();}).catch(function(e){modalError(error,e);}).finally(function(){value=null;submit.disabled=false;});},true);
   modal('授权 '+row.name,[E('p',{class:'rp-help'},'打开官方团队页面登录，再将登录完成的授权链接粘贴到下方。令牌可能很快过期，请及时提交。若浏览器提示打开 WARP／Cloudflare One，请先取消，只复制授权链接到这里，避免改变其他客户端的登录。'),E('a',{class:'btn',href:url,target:'_blank',rel:'noopener noreferrer'},'打开 Cloudflare 官方授权页'),field('官方授权链接或团队 JWT',token),E('label',{class:'rp-inline-check'},[terms,E('span',{},['我已阅读并同意 ',E('a',{href:'https://www.cloudflare.com/application/terms/',target:'_blank',rel:'noopener noreferrer'},'Cloudflare 服务条款'),'，授权创建此独立注册。'])]),E('p',{class:'rp-help'},'此适配器使用第三方客户端注册协议。组织要求设备姿态、硬件密钥或不允许第三方客户端时，可能无法注册；不会自动更改组织策略。'),error,modalActions([button('取消',function(){token.value='';ui.hideModal();}),submit])]);
  }
  function confirmAction(row,action){
   var name=E('input',{type:'text',autocomplete:'off'}),error=E('p',{hidden:true,role:'alert',class:'imok-client-error'}),title=action==='revoke'?'撤销云端注册':action==='stop_subscription'?'停止私密订阅':'删除本地记录';
   var submit=button('确认'+title,function(){submit.disabled=true;request(action,{id:row.id,confirm:name.value}).then(function(){dismiss(submit);if(!root.isConnected)return;message(action==='revoke'?'撤销任务已提交，正在等待云端确认。':action==='stop_subscription'?'正在移除云端订阅内容；等待旧链接失效确认。':'本地记录已删除。');if(action==='delete')selected=null;return refresh();}).catch(function(e){modalError(error,e);}).finally(function(){submit.disabled=false;});});submit.classList.add('cbi-button-negative');
   modal(title,[E('p',{},action==='revoke'?'撤销后，此客户端已经导出的配置将失效。只操作本模块创建的这个身份，不撤销路由器或其他设备。':action==='stop_subscription'?'停止后旧订阅地址将失效，已导入的节点仍可连接。再次创建会生成新链接；需要使节点本身失效时，应撤销注册。':'只有未注册或已确认撤销的记录可以删除。'),field('输入客户端名称「'+row.name+'」确认',name),error,modalActions([button('取消',function(){ui.hideModal();}),submit])]);
  }
  function publishSubscription(row){
   var error=E('p',{hidden:true,role:'alert',class:'imok-client-error'});
   var submit=button('创建私密订阅',function(){submit.disabled=true;request('publish_subscription',{id:row.id,confirm:true}).then(function(){dismiss(submit);if(!root.isConnected)return;message('私密订阅任务已提交，等待 HTTPS 内容读回确认；未更改手机当前连接。');return refresh();}).catch(function(e){modalError(error,e);}).finally(function(){submit.disabled=false;});},true);
   modal('创建 '+row.name+' 的私密订阅',[E('p',{},'使用 Workers 页面已保存的 Cloudflare API 授权，新建独立的订阅 Worker。只把此手机的连接配置保存在云端私密 Secret 中，不上传路由器身份或完整备份，不改备用线路。'),E('p',{class:'rp-help'},'订阅地址具有读取此手机私钥的权限，请只交给对应设备。imOK 保存端口、入口 IP 或 DNS 后会更新订阅；手机需要更新订阅后重连。'),error,modalActions([button('取消',function(){ui.hideModal();}),submit])]);
  }
  function exportClient(row,mode){
   mode=mode||'direct';
   if(dirty(row)){message('连接参数尚未保存，请先保存后再导出。',true);var save=detail.querySelector('[data-client-save]');if(save)save.focus();return;}
   var output=E('textarea',{readonly:true,rows:4,spellcheck:'false','aria-label':mode==='subscription'?'可读取手机私钥的私密订阅链接':'包含私钥的客户端导入链接'}),qr=E('div',{class:'imok-client-qr'}),error=E('p',{hidden:true,role:'alert',class:'imok-client-error'}),secret='',timer,observer;
   function clear(){secret='';output.value='';qr.replaceChildren();if(timer)clearTimeout(timer);if(observer)observer.disconnect();document.removeEventListener('visibilitychange',hidden);document.removeEventListener('keydown',escape,true);if(exportCleanup===clear)exportCleanup=null;}
   function escape(event){if(event.key==='Escape')clear();}
   function close(){var current=currentModal(output);clear();if(current)ui.hideModal();}
   function hidden(){if(document.hidden){clear();output.placeholder='已清除显示，请关闭后重新导出。';}}
   var reveal=button(mode==='subscription'?'确认显示私密订阅与二维码':'确认显示私钥与二维码',function(){reveal.disabled=true;request(mode==='subscription'?'subscription_link':'export',{id:row.id,confirm:true}).then(function(v){if(!currentModal(output)||document.hidden)return;secret=v.uri;output.value=secret;var code=qrcode(0,'M');code.addData(secret);code.make();var image=E('img',{src:code.createDataURL(4,16),alt:'客户端私密导入二维码'});qr.replaceChildren(image);reveal.hidden=true;timer=setTimeout(function(){clear();output.placeholder='显示时间已到，请关闭后重新导出。';},120000);document.addEventListener('visibilitychange',hidden);document.addEventListener('keydown',escape,true);observer=new MutationObserver(function(){if(!output.isConnected||!output.getClientRects().length)clear();});observer.observe(document.documentElement,{childList:true,attributes:true,attributeFilter:['class','style','open'],subtree:true});exportCleanup=clear;}).catch(function(e){modalError(error,e);reveal.disabled=false;});},true);
   var download=button('下载导入文件',function(){if(!secret){modalError(error,Error('请先确认显示导入配置'));return;}var blob=new Blob([secret+'\n'],{type:'text/plain;charset=utf-8'}),url=URL.createObjectURL(blob),a=E('a',{href:url,download:mode==='subscription'?'imok-subscription-'+row.id.slice(0,8)+'.txt':'imok-client-'+row.id.slice(0,8)+'.txt'});document.body.appendChild(a);a.click();a.remove();setTimeout(function(){URL.revokeObjectURL(url);},1000);});
   modal((mode==='subscription'?'私密订阅 · ':'直接导入 · ')+row.name,[E('p',{},mode==='subscription'?'订阅地址可读取此手机的私钥，请只交给对应设备。作为订阅添加到小火箭；imOK 更新参数后，在手机更新订阅并重连。页面两分钟后清除显示。':'导入链接和二维码含此客户端私钥，请只交给对应设备。页面两分钟后清除显示；直接导入后，端口等参数变化需重新导入。'),E('p',{class:'rp-help'},'云端出口（上次同步）：'+(row.network&&row.network.selected_name||'尚未同步')+'。修改云端出口后，请在手机断开再连接；手机实际出口需要另行验证。'),error,reveal,qr,output,modalActions([button('关闭并清除',close),download])]);
  }
  function draw(){
   var focused=document.activeElement,focusedName=root.contains(focused)&&focused.name,selection=focusedName?[focused.selectionStart,focused.selectionEnd]:null;
   if(exportCleanup&&!root.isConnected)exportCleanup();
   list.replaceChildren();if(!data.clients.some(function(r){return r.id===selected;}))selected=data.clients.length?data.clients[0].id:null;
   if(data.load_error){message(data.load_error,true);list.appendChild(E('p',{role:'alert'},'客户端列表读取失败'));detail.replaceChildren(panel('暂时无法读取客户端','现有身份未重置',[E('p',{},'请检查权限或连接后重新读取。'),button('重新读取',function(){refresh(true);})]));return;}
   if(!data.clients.length){list.appendChild(E('div',{class:'imok-client-empty'},[E('strong',{},'还没有客户端'),E('p',{class:'rp-help'},'新增一个客户端，为手机获取独立的注册身份。')]));detail.replaceChildren(panel('开始接入','保留路由器当前线路',[E('ol',{class:'imok-client-steps'},[E('li',{},'创建客户端记录，填写团队名称。'),E('li',{},'完成官方登录，申请独立身份和 IP。'),E('li',{},'核对连接参数，扫码或下载导入小火箭。')]),E('p',{class:'rp-help'},'完成授权后，同步团队出口目录，选择并应用出口，再导入小火箭。服务令牌自动注册和手机隧道检测尚未开放。')]));return;}
   data.clients.forEach(function(row){list.appendChild(E('button',{type:'button',class:'imok-client-item btn','aria-pressed':row.id===selected?'true':'false',click:function(event){selected=row.id;draw();if(event.detail===0)list.querySelector('[aria-pressed=true]').focus();}},[E('strong',{},row.name),E('span',{},row.ipv4||row.organization),E('small',{},row.label)]));});
   var row=data.clients.filter(function(r){return r.id===selected;})[0],cfg=drafts[row.id]||row.settings,readOnly=E('dl',{class:'imok-client-facts'},[['团队',row.organization],['虚拟 IPv4',row.ipv4||'注册后分配'],['虚拟 IPv6',row.ipv6||'尚未分配'],['手机连接状态','需在手机客户端查看'],['云端出口选择',row.network&&row.network.selected_name?(row.network.selected_name+(row.network.state==='confirmed'?'':'（上次读取）')):'尚未同步'],['注册资料核验时间',row.last_checked_at?new Date(row.last_checked_at*1000).toLocaleString():'尚未核验']].map(function(x){return E('div',{},[E('dt',{},x[0]),E('dd',{},x[1])]);}));
   var actions=[];if(!row.has_registration&&['draft','failed'].indexOf(row.state)>=0)actions.push(button('授权并注册',function(){registerClient(row);},true));if(row.has_registration&&row.state!=='revoked'){actions.push(button('同步凭据与出口',function(){request('refresh',{id:row.id}).then(function(){message('同步任务已提交，正在核对注册资料与云端出口。');return refresh();}).catch(function(e){message(e.message,true);});}));}
   var state=E('span',{class:'imok-client-state','data-state':row.state},row.label),summary=panel(row.name,'管理此设备的注册资料与出口；手机连接状态请在客户端查看。',[state,readOnly,row.error?E('p',{class:'imok-client-error',role:'alert'},row.error):'',E('div',{class:'rp-backup-actions'},actions),row.fingerprint?E('details',{class:'imok-client-technical'},[E('summary',{},'身份与核对信息'),E('p',{},'云端注册 ID：'+(row.registration_id||'尚未确认')),E('p',{},'MASQUE 公钥指纹（本地生成）'),E('code',{},row.fingerprint),row.bootstrap_fingerprint?E('div',{},[E('p',{},'预注册公钥指纹（用于结果未知时核对）'),E('code',{},row.bootstrap_fingerprint)]):'']):'']);
   var network=row.network||{state:'unread',selected_id:'',selected_name:'',networks:[]},options=network.networks.filter(function(n){return !n.is_default;}),chosen=E('select',{name:'client-exit','aria-label':'此客户端使用的出口'},[E('option',{value:''},'请选择出口')].concat(options.map(function(n){return E('option',{value:n.id},n.name);}))),applyButton;
   chosen.value=exitDrafts[row.id]||network.selected_id;if(!options.some(function(n){return n.id===chosen.value;}))chosen.value='';
   function updateApply(){applyButton.disabled=row.busy||network.state!=='confirmed'||!chosen.value||chosen.value===network.selected_id;}
   chosen.addEventListener('change',function(){exitDrafts[row.id]=chosen.value;updateApply();});
   applyButton=button('应用此客户端出口',function(){if(!chosen.value)return;request('select_exit',{id:row.id,vnet_id:chosen.value,expected_vnet_id:network.selected_id}).then(function(){delete exitDrafts[row.id];message('出口设置任务已提交，等待云端读回确认。只影响此客户端，手机需断开再连接。');return refresh();}).catch(function(e){message(e.message,true);});},true);updateApply();
   var exitInfo=network.state==='confirmed'?(options.some(function(n){return n.id===network.selected_id;})?'云端已确认选择：'+network.selected_name+'。这不代表手机实际出口已检测。':'当前仍是默认网络；请先选择并应用指定出口，再导出。'):network.state==='pending'||network.state==='unknown'?'出口操作结果待核对，请同步凭据与出口；不要重复应用。':'请先点击“同步凭据与出口”，读取此身份可用的团队出口。';
   var exitPanel=panel('客户端出口','读取同一团队的云端出口目录；不会跟随路由器切换线路。',[E('p',{class:'rp-help',role:'status'},exitInfo),network.error?E('p',{class:'imok-client-error',role:'alert'},network.error):'',field('选择出口',chosen,'默认网络不作为可选出口。目录名称与选择状态随同步更新。'),E('div',{class:'rp-backup-actions'},[applyButton]),E('p',{class:'rp-help'},'应用会更新这个独立身份的云端选择，已导出的链接不必换密钥，重连后生效。入口 IP、端口或 DNS 改动后，按接入方式重新导入或更新订阅。')]);
   var endpoint=E('input',{type:'text',name:'client-endpoint',value:cfg.endpoint}),port=E('select',{name:'client-port'},[443,500,1701,4500,4443,8443,8095].map(function(p){return E('option',{value:String(p)},String(p));})),dns=E('input',{type:'text',name:'client-dns',value:cfg.dns}),mtu=E('input',{type:'number',name:'client-mtu',min:1280,max:1420,value:String(cfg.mtu)}),keepalive=E('input',{type:'number',name:'client-keepalive',min:10,max:120,value:String(cfg.keepalive)});port.value=String(cfg.port);
   function draft(){drafts[row.id]={endpoint:endpoint.value,port:port.value,dns:dns.value,mtu:mtu.value,keepalive:keepalive.value,cc:cfg.cc};}
   [endpoint,port,dns,mtu,keepalive].forEach(function(input){input.addEventListener('input',draft);input.addEventListener('change',draft);});
   var sub=row.subscription||{state:'none',enabled:false,needs_update:false,synced_at:0};
   var saveButton=button(sub.enabled?'保存并更新订阅':'保存导出参数',function(){var saved=false;request('edit',{id:row.id,settings:{endpoint:endpoint.value,port:port.value,dns:dns.value,mtu:mtu.value,keepalive:keepalive.value,cc:cfg.cc}}).then(function(v){saved=true;delete drafts[row.id];if(v.client.subscription&&v.client.subscription.enabled)return request('publish_subscription',{id:row.id,confirm:true}).then(function(){message('参数已保存，正在更新私密订阅并核对内容；手机需更新订阅后重连。');});message('导出参数已保存；直接导入的配置需重新导入。');}).then(function(){return refresh();}).catch(function(e){message((saved?'参数已保存，但订阅未确认更新：':'')+e.message,true);return refresh(false);});});
   saveButton.setAttribute('data-client-save','');
   var settingsPanel=panel('连接参数','保存后同步已启用的订阅；手机更新订阅或重新导入后生效。',[E('div',{class:'imok-client-fields'},[field('Cloudflare 入口 IPv4',endpoint,'从官方 Zero Trust MASQUE 地址范围选择。'),field('UDP 端口',port,'默认 443；端口不决定出口国家。'),field('DNS',dns,'可修改；私有域名需要对应的内部解析配置。')]),E('details',{class:'imok-client-advanced'},[E('summary',{},'高级连接参数'),E('div',{class:'imok-client-fields'},[field('MTU',mtu),field('心跳间隔（秒）',keepalive)]),E('p',{class:'rp-help'},'SNI：zt-masque.cloudflareclient.com · 拥塞控制：cubic · UDP 转发开启 · HTTP2 关闭')]),E('div',{class:'rp-backup-actions'},[saveButton]),E('p',{class:'rp-help'},'出口由上方云端网络选择决定，端口和节点名称不决定地区。保存导出参数不会替代“应用此客户端出口”。')]);
   var subscriptionActions=[];
   if(row.exportable){subscriptionActions.push(button('直接导入／二维码',function(){exportClient(row);},true));if(sub.enabled){subscriptionActions.push(button('更新私密订阅',function(){request('publish_subscription',{id:row.id,confirm:true}).then(function(){message('正在更新订阅，等待 HTTPS 内容核对；手机尚未自动获取。');return refresh();}).catch(function(e){message(e.message,true);});}));if(sub.state==='published'&&!sub.needs_update)subscriptionActions.push(button('显示私密订阅链接',function(){exportClient(row,'subscription');},true));}else subscriptionActions.push(button('创建私密订阅',function(){publishSubscription(row);}));}
   if(sub.enabled)subscriptionActions.push(button('停止私密订阅',function(){confirmAction(row,'stop_subscription');}));
   var subLabel=sub.state==='published'?(sub.needs_update?'参数有变化，订阅待更新':'订阅内容已核对；手机是否已更新尚未检测'):sub.state==='none'||sub.state==='disabled'?('尚未启用固定订阅。'+(row.exportable?'可直接导入。':'完成注册并确认出口后，可选择接入方式。')):sub.state==='unknown'?'订阅结果待核对，原链接保留。':'正在准备或更新订阅。';
   var accessPanel=panel('接入方式','同一独立身份，保留两种接入方式。',[E('div',{class:'imok-client-fields'},[E('div',{},[E('strong',{},'直接导入／二维码'),E('p',{class:'rp-help'},'一次性导入。端口、入口 IP、DNS 改动后，需要重新导入。')]),E('div',{},[E('strong',{},'固定私密订阅'),E('p',{class:'rp-help'},'固定地址，内容可更新。启用后，保存参数会同步订阅；手机需更新订阅后获取。')])]),E('p',{class:'rp-help',role:'status'},subLabel),sub.synced_at?E('p',{class:'rp-help'},'最近云端核对：'+new Date(sub.synced_at*1000).toLocaleString()):'',sub.error?E('p',{class:'imok-client-error',role:'alert'},sub.error):'',E('div',{class:'rp-backup-actions'},subscriptionActions),E('p',{class:'rp-help'},'两种方式切换美国／首尔出口均无需重新申请身份，应用出口后手机重连即可。停止订阅不会撤销已导入的节点。')]);
   var danger=E('details',{class:'imok-client-advanced'},[E('summary',{},'撤销与删除'),E('p',{class:'rp-help'},'撤销会使已导出的配置失效。删除本地记录不会代替云端撤销。'),E('div',{class:'rp-backup-actions'},[row.has_registration&&row.state!=='revoked'?button('撤销此客户端注册',function(){confirmAction(row,'revoke');}):'',button('删除本地记录',function(){confirmAction(row,'delete');})])]);
   detail.replaceChildren(summary,E('div',{class:'imok-client-methods'},[exitPanel,accessPanel]),settingsPanel,danger);if(focusedName){var target=detail.querySelector('[name="'+focusedName+'"]');if(target){target.focus();try{if(selection&&selection[0]!=null)target.setSelectionRange(selection[0],selection[1]);}catch(ignore){}}}if(row.busy){detail.querySelectorAll('button').forEach(function(b){b.disabled=true;});}
  }
  draw();poll.add(function(){if(!root.isConnected){if(exportCleanup)exportCleanup();return Promise.resolve();}if(data.clients.some(function(r){return r.busy;})&&!inFlight)return refresh(false);return Promise.resolve();},2);return root;
 }
});
