package actions

import (
	"fmt"

	"github.com/elastic/beats/v7/libbeat/beat"
	"github.com/elastic/beats/v7/libbeat/common"
)

func getMapStrFields(e *beat.Event) (common.MapStr, error) {
	m, ok := e.Fields.(common.MapStr)
	if !ok {
		return nil, fmt.Errorf("common.MapStr required, but got %T", e.Fields)
	}
	return m, nil
}
