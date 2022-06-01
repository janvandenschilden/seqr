import React from 'react'
import styled from 'styled-components'

import { FontAwesomeIconsContainer } from '../StyledComponents'

const IdeogramContainer = styled(FontAwesomeIconsContainer)`
  .fa-lg {
    line-height: 1;
    vertical-align: middle;
    font-size: 1.5em;
  }
  
  .Ideogram-zoom-widget i {
    line-height: 24px;
  }
`

class Ideogram extends React.PureComponent {
  
  render() {
    return <div>Ideogram</div>
  }

}

export default Ideogram
